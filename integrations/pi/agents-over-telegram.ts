/**
 * Agents over Telegram: push delivery for Pi.
 *
 * Pi gets the Telegram tools from the project's `.pi/mcp.json`, which
 * `aot setup --host pi` writes. This extension only adds push delivery: while
 * a Pi session runs in a configured project, it runs `aot watch --collect` and
 * turns each "new messages" event into a message for the agent. Pi steers a
 * busy agent with it and starts a turn when the agent is idle.
 *
 * Notices never contain Telegram message text. The agent reads messages with
 * its scoped Telegram tools (messages_poll), replies (message_send) and
 * acknowledges them (messages_ack).
 *
 * The project selects its runtime through `.aot-runtime.json`, written by
 * setup. This extension reads only that file in the session's exact folder.
 */
import { spawn, type ChildProcess } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const POINTER = ".aot-runtime.json";
const PROFILE = ".aot-profile.json";
const MESSAGE_TYPE = "agents-over-telegram";
const MAX_LINE = 64 * 1024;
const RESTART_DELAYS_MS = [2_000, 5_000, 15_000, 30_000, 60_000];
const FATAL_REASONS = new Set(["client_revoked", "unknown_client", "authentication_failed", "not_connected", "account_not_registered"]);

function ownedPrivateFile(file: string): boolean {
	try {
		const info = fs.lstatSync(file);
		const uid = typeof process.getuid === "function" ? process.getuid() : info.uid;
		return info.isFile() && info.uid === uid && (info.mode & 0o022) === 0 && info.nlink === 1;
	} catch {
		return false;
	}
}

/** The project's prepared `aot` executable, or undefined when this folder is not set up. */
export function runtimeExecutable(project: string): string | undefined {
	const pointer = path.join(project, POINTER);
	if (!ownedPrivateFile(pointer) || !ownedPrivateFile(path.join(project, PROFILE))) return undefined;
	try {
		const value = JSON.parse(fs.readFileSync(pointer, { encoding: "utf8" }).slice(0, 65_536));
		const keys = value && typeof value === "object" ? Object.keys(value).sort().join(",") : "";
		if (keys !== "executable,version" || value.version !== 1) return undefined;
		const executable = value.executable;
		if (typeof executable !== "string" || !path.isAbsolute(executable) || executable.includes("\0")) return undefined;
		fs.accessSync(executable, fs.constants.X_OK);
		const info = fs.statSync(executable);
		const uid = typeof process.getuid === "function" ? process.getuid() : info.uid;
		// Same rule as scripts/aot_host_launch.py: owned by this user or root, not world-writable.
		if (!info.isFile() || (info.uid !== uid && info.uid !== 0) || (info.mode & 0o002) !== 0) return undefined;
		return executable;
	} catch {
		return undefined;
	}
}

export default function (pi: ExtensionAPI) {
	let child: ChildProcess | undefined;
	let restartTimer: ReturnType<typeof setTimeout> | undefined;
	let stopped = true;
	let failures = 0;
	// Each start gets a new generation. Callbacks from an older watcher see a
	// different generation and do nothing, so a session restart cannot leave
	// two watchers running.
	let generation = 0;

	const stop = () => {
		generation += 1;
		stopped = true;
		if (restartTimer) clearTimeout(restartTimer);
		restartTimer = undefined;
		if (child && child.exitCode === null) child.kill("SIGTERM");
		child = undefined;
	};

	const deliver = (notice: string) => {
		pi.sendMessage(
			{ customType: MESSAGE_TYPE, content: notice, display: true },
			{ triggerTurn: true, deliverAs: "steer" },
		);
	};

	const start = (project: string, notify: (text: string) => void) => {
		const executable = runtimeExecutable(project);
		if (!executable) return;
		stopped = false;
		failures = 0;
		const current = ++generation;
		const active = () => !stopped && generation === current;
		const run = () => {
			if (!active()) return;
			const proc = spawn(executable, ["watch", "--collect", "--format", "jsonl"], {
				cwd: project,
				stdio: ["ignore", "pipe", "ignore"],
			});
			child = proc;
			let buffer = "";
			let fatal = false;
			proc.stdout?.setEncoding("utf8");
			proc.stdout?.on("data", (chunk: string) => {
				buffer += chunk;
				if (buffer.length > MAX_LINE * 4) buffer = buffer.slice(-MAX_LINE);
				let newline = buffer.indexOf("\n");
				while (newline >= 0) {
					const line = buffer.slice(0, newline).trim();
					buffer = buffer.slice(newline + 1);
					newline = buffer.indexOf("\n");
					if (!line || line.length > MAX_LINE) continue;
					let event: any;
					try {
						event = JSON.parse(line);
					} catch {
						continue;
					}
					if (!active()) return;
					if (event?.type === "telegram_messages" && typeof event.notice === "string") {
						failures = 0;
						deliver(event.notice);
					} else if (event?.type === "watch_stopped") {
						fatal = FATAL_REASONS.has(String(event.reason));
						notify(`Agents over Telegram stopped watching: ${String(event.reason).slice(0, 64)}`);
					}
				}
			});
			proc.on("error", () => {});
			proc.on("exit", () => {
				if (child === proc) child = undefined;
				if (!active() || fatal) return;
				// Restart a watcher that died unexpectedly, with a growing delay.
				const delay = RESTART_DELAYS_MS[Math.min(failures, RESTART_DELAYS_MS.length - 1)];
				failures += 1;
				restartTimer = setTimeout(run, delay);
			});
		};
		run();
	};

	pi.on("session_start", async (_event, ctx) => {
		stop();
		process.removeListener("exit", stop);
		process.once("exit", stop);
		start(ctx.cwd, (text) => {
			if (ctx.hasUI) ctx.ui.notify(text, "warning");
		});
	});

	pi.on("session_shutdown", async () => {
		stop();
		process.removeListener("exit", stop);
	});
}
