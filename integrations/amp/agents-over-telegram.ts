/**
 * Agents over Telegram: push delivery for Amp.
 *
 * Amp gets the Telegram tools from the workspace `.amp/settings.json`, which
 * `aot setup --host amp` writes (approve it once with `amp mcp approve telegram`).
 * This plugin only adds push delivery: while Amp runs in a configured workspace,
 * it runs `aot watch --collect` and appends each "new messages" notice to the
 * current thread as a steering user message. Amp queues it while the thread is
 * busy and runs it when the thread is idle.
 *
 * Notices never contain Telegram message text. The agent reads messages with
 * its scoped Telegram tools (messages_poll), replies (message_send) and
 * acknowledges them (messages_ack).
 *
 * The workspace selects its runtime through `.aot-runtime.json`, written by
 * setup. This plugin reads only that file in the exact workspace root.
 */
import { spawn, type ChildProcess } from 'node:child_process'
import * as fs from 'node:fs'
import * as path from 'node:path'
import type { PluginAPI } from '@ampcode/plugin'

export const description = 'Wakes the current thread when new Agents over Telegram messages arrive (notices only).'

const POINTER = '.aot-runtime.json'
const PROFILE = '.aot-profile.json'
const MAX_LINE = 64 * 1024
const MAX_QUEUED = 20
const RESTART_DELAYS_MS = [2_000, 5_000, 15_000, 30_000, 60_000]
const FATAL_REASONS = new Set(['client_revoked', 'unknown_client', 'authentication_failed', 'account_not_registered'])

function ownedPrivateFile(file: string): boolean {
	try {
		const info = fs.lstatSync(file)
		const uid = typeof process.getuid === 'function' ? process.getuid() : info.uid
		return info.isFile() && info.uid === uid && (info.mode & 0o022) === 0 && info.nlink === 1
	} catch {
		return false
	}
}

/** The workspace's prepared `aot` executable, or undefined when it is not set up. */
export function runtimeExecutable(project: string): string | undefined {
	const pointer = path.join(project, POINTER)
	if (!ownedPrivateFile(pointer) || !ownedPrivateFile(path.join(project, PROFILE))) return undefined
	try {
		const value = JSON.parse(fs.readFileSync(pointer, { encoding: 'utf8' }).slice(0, 65_536))
		const keys = value && typeof value === 'object' ? Object.keys(value).sort().join(',') : ''
		if (keys !== 'executable,version' || value.version !== 1) return undefined
		const executable = value.executable
		if (typeof executable !== 'string' || !path.isAbsolute(executable) || executable.includes('\0')) return undefined
		fs.accessSync(executable, fs.constants.X_OK)
		return fs.statSync(executable).isFile() ? executable : undefined
	} catch {
		return undefined
	}
}

export default function (amp: PluginAPI) {
	const root = amp.system.workspaceRoot
	if (!root) return
	const project = amp.helpers.filePathFromURI(root)
	const executable = runtimeExecutable(project)
	if (!executable) return

	let child: ChildProcess | undefined
	let restartTimer: ReturnType<typeof setTimeout> | undefined
	let stopped = false
	let failures = 0
	let threadID: string | undefined = amp.activeThread.current?.id
	const queued: string[] = []

	let opening: Promise<string | undefined> | undefined
	const openThread = async (): Promise<string | undefined> => {
		// Amp has no thread until the first message. Open one, shown to the user,
		// so a freshly started idle Amp still handles Telegram work.
		try {
			const thread: any = await amp.getBuiltinAgent('medium').createThread({ show: true } as any)
			return typeof thread?.id === 'string' ? thread.id : undefined
		} catch (error) {
			amp.logger.log(`agents-over-telegram: could not open a thread: ${String(error).slice(0, 200)}`)
			return undefined
		}
	}

	const deliver = async (notice: string) => {
		let target = amp.activeThread.current?.id ?? threadID
		if (!target) {
			opening ??= openThread()
			target = await opening
			opening = undefined
			if (target) threadID = target
		}
		if (!target) {
			// Deliver when the first thread session starts.
			if (queued.length < MAX_QUEUED) queued.push(notice)
			return
		}
		try {
			await amp.threads.get(target as any).appendUserMessage({ type: 'user-message', content: notice }, { steer: true })
		} catch (error) {
			amp.logger.log(`agents-over-telegram: could not append notice: ${String(error).slice(0, 200)}`)
		}
	}

	amp.on('session.start', async (event) => {
		threadID = event.thread.id
		while (queued.length) await deliver(queued.shift() as string)
	})

	const run = () => {
		if (stopped) return
		const proc = spawn(executable, ['watch', '--collect', '--format', 'jsonl'], {
			cwd: project,
			stdio: ['ignore', 'pipe', 'ignore'],
		})
		child = proc
		let buffer = ''
		let fatal = false
		proc.stdout?.setEncoding('utf8')
		proc.stdout?.on('data', (chunk: string) => {
			buffer += chunk
			if (buffer.length > MAX_LINE * 4) buffer = buffer.slice(-MAX_LINE)
			let newline = buffer.indexOf('\n')
			while (newline >= 0) {
				const line = buffer.slice(0, newline).trim()
				buffer = buffer.slice(newline + 1)
				newline = buffer.indexOf('\n')
				if (!line || line.length > MAX_LINE) continue
				let event: any
				try {
					event = JSON.parse(line)
				} catch {
					continue
				}
				if (event?.type === 'telegram_messages' && typeof event.notice === 'string') {
					failures = 0
					void deliver(event.notice)
				} else if (event?.type === 'watch_stopped') {
					fatal = FATAL_REASONS.has(String(event.reason))
					amp.logger.log(`agents-over-telegram: watcher stopped: ${String(event.reason).slice(0, 64)}`)
				}
			}
		})
		proc.on('error', () => {})
		proc.on('exit', () => {
			if (child === proc) child = undefined
			if (stopped || fatal) return
			const delay = RESTART_DELAYS_MS[Math.min(failures, RESTART_DELAYS_MS.length - 1)]
			failures += 1
			restartTimer = setTimeout(run, delay)
		})
	}

	amp.onDispose(() => {
		stopped = true
		if (restartTimer) clearTimeout(restartTimer)
		if (child && child.exitCode === null) child.kill('SIGTERM')
	})

	run()
}
