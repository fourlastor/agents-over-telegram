#!/usr/bin/env python3
"""Offline end-to-end harness: real agent hosts against a fake Telegram.

The fake transport is a JSON file inside an isolated state directory. Real host
processes (Claude Code, Codex, Pi, Amp) run the real ``aot`` runtime against it,
so a test can inject a Telegram update and check what the agent sent back.
Nothing here reads a bot token or contacts Telegram.

Commands (run with this checkout's ``.venv/bin/python``):

  prepare --root DIR --host HOST [--label NAME]
      Create DIR/state (fake mode) and DIR/project, run ``aot setup --host HOST``,
      pair the project to a fake group topic and print the environment as JSON.
  inject  --root DIR --text TEXT [--sender-id N] [--username NAME]
      Append one fake Telegram message from an allowed human to the paired topic.
  sent    --root DIR
      Print messages the agent published through the fake bot.
  inbox   --root DIR
      Print pending/acknowledged counts for the project's client.
  wait-sent --root DIR --count N [--timeout S]
      Wait until at least N messages were published; exit 1 on timeout.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
AOT = ROOT / ".venv" / "bin" / "aot"
CHAT_ID, THREAD_ID, OWNER_ID = -100777, 42, 4242


def _aot(state: Path, *args: str, cwd: Path | None = None) -> dict:
    result = subprocess.run([str(AOT), "--state-dir", str(state), "--fake", *args],
                            capture_output=True, text=True, cwd=cwd, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise SystemExit(f"aot {' '.join(args)} failed: {result.stderr.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def _fake(state: Path) -> Path:
    return state / "fake-transport.json"


def _update(state: Path, text: str, sender_id: int, username: str, is_bot: bool = False) -> int:
    sys.path.insert(0, str(ROOT / "src"))
    from agents_over_telegram.transport import FileFakeTransport
    transport = FileFakeTransport(_fake(state))
    with transport._data() as data:
        update_id = max([u["update_id"] for u in data["updates"]] + [0]) + 1
        data["updates"].append({"update_id": update_id, "message": {
            "message_id": 1000 + update_id, "date": int(time.time()),
            "chat": {"id": CHAT_ID, "type": "supergroup", "title": "AOT e2e"},
            "message_thread_id": THREAD_ID,
            "from": {"id": sender_id, "is_bot": is_bot, "username": username}, "text": text}})
    return update_id


def prepare(args) -> dict:
    root = Path(args.root).resolve()
    state, project = root / "state", root / "project"
    project.mkdir(parents=True, exist_ok=True)
    for path in (root, state):
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    result = _aot(state, "setup", "--label", args.label, "--project-dir", str(project),
                  "--host", args.host, "--prepare-only")
    if not result.get("connected"):
        command = result["pairing_command"]
        _update(state, command, OWNER_ID, "e2e_owner")
        _aot(state, "collect")
        _aot(state, "pair-confirm", "--pairing-id", result["pairing_id"], "--confirm")
    status = _aot(state, "status", cwd=project)
    return {"root": str(root), "state_dir": str(state), "project_dir": str(project),
            "aot": str(AOT), "host": args.host, "label": args.label,
            "connected": status["connected"], "destination": status["destination"],
            "fake_transport": str(_fake(state))}


def sent(root: Path) -> list:
    path = _fake(root / "state")
    if not path.exists():
        return []
    return json.loads(path.read_text()).get("sent", [])


def inbox(root: Path) -> dict:
    status = _aot(root / "state", "status", cwd=root / "project")
    return {"pending_messages": status["pending_messages"], "outbox": status["outbox"],
            "state": status["state"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--root", required=True)
    p.add_argument("--host", required=True, choices=["codex", "claude", "pi", "amp"])
    p.add_argument("--label", default="E2E Agent")
    i = sub.add_parser("inject")
    i.add_argument("--root", required=True)
    i.add_argument("--text", required=True)
    i.add_argument("--sender-id", type=int, default=OWNER_ID)
    i.add_argument("--username", default="e2e_owner")
    for name in ("sent", "inbox"):
        sub.add_parser(name).add_argument("--root", required=True)
    w = sub.add_parser("wait-sent")
    w.add_argument("--root", required=True)
    w.add_argument("--count", type=int, default=1)
    w.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    if args.command == "prepare":
        print(json.dumps(prepare(args), indent=2))
    elif args.command == "inject":
        print(json.dumps({"update_id": _update(Path(args.root).resolve() / "state", args.text,
                                               args.sender_id, args.username)}))
    elif args.command == "sent":
        print(json.dumps(sent(Path(args.root).resolve()), indent=2))
    elif args.command == "inbox":
        print(json.dumps(inbox(Path(args.root).resolve()), indent=2))
    elif args.command == "wait-sent":
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            messages = sent(Path(args.root).resolve())
            if len(messages) >= args.count:
                print(json.dumps(messages, indent=2))
                return 0
            time.sleep(1)
        print(json.dumps({"timeout": True, "sent": sent(Path(args.root).resolve())}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
