---
name: telegram-setup
description: Connect this agent or project to Telegram with Agents over Telegram in any supported host (Claude Code, Codex, Pi, Amp, Antigravity). Reuses the human's existing bot; the human only posts one pairing command in the group.
---

Use this skill for "connect this agent to Telegram" and for updates. The human installed Agents over Telegram (AOT) once for their host. That install brought this skill and the host's hooks, extension or plugin. Your job is to set up the current project. The human's part is to post one pairing command in their Telegram group. For a bot that was never registered on this machine, they also enter its token once in their own terminal.

One project folder is one logical agent: every chat or session in that exact folder shares its Telegram identity. Explain this once. If the user needs separate identities, use separate project folders. Unrelated bots and bridges stay outside the task.

If this skill's installed package includes a root `mcp.json` or `.mcp.json`, it is an advanced rendered profile that is already connected. Check its `connection_status`, report any loading problem, and do not run setup.

## Connect the project

1. **Find the project and label.** Use the exact project folder of this session and a short logical label (for example the project name). Reuse an existing `.aot-profile.json` in that exact folder. Never search parent folders for one, and never pick another project's client.

2. **Choose the host.** Use the host you are running in, unless the user names another one:

   | Host | `--host` | What setup writes in the project |
   |---|---|---|
   | Claude Code | `claude` | Nothing else: the plugin's MCP server finds the project |
   | Codex | `codex` | The `telegram` entry in `.codex/config.toml`, with its four push tools approved |
   | Pi | `pi` | `.pi/mcp.json` |
   | Amp | `amp` | `.amp/settings.json` (approve it once with `amp mcp approve telegram`) |
   | Antigravity (`agy` CLI or desktop app) | `antigravity` | `.agents/plugins/agents-over-telegram-project/` with the MCP server |

   Every host also gets `.aot-profile.json` and `.aot-runtime.json`. Setup adds the files AOT owns to the repository's local `.git/info/exclude`. They hold this machine's paths and the client id, not secrets, and they must not be committed. When the setup result has a `git.warning`, tell the user in one line which files git tracks.

3. **Prepare the runtime.** If the project already has a configured runtime (`.aot-runtime.json` or the host's `telegram` entry), keep it: ordinary onboarding never moves a project to another runtime. Otherwise find `scripts/bootstrap_runtime.py`:
   - Next to this skill's package, at `../../scripts/bootstrap_runtime.py`. This is the case when the skill came with the whole repository (Claude Code, Codex, Pi, Antigravity).
   - Otherwise (Amp copies only the skill folder), in `~/.local/share/agents-over-telegram/source`. If that folder does not exist, clone it there with `git clone --depth 1 https://github.com/fourlastor/agents-over-telegram ~/.local/share/agents-over-telegram/source`. If it exists, update it with `git -C ~/.local/share/agents-over-telegram/source pull --ff-only`.

   Run the script with a Python 3.11 or newer interpreter. Check the version; the first `python3` on `PATH` may be older:

   ```sh
   "/absolute/path/to/python3.11-or-newer" "/absolute/path/to/scripts/bootstrap_runtime.py"
   ```

   It prints JSON with `status: "ready"` and the absolute `executable` to use. It uses uv when present, otherwise Python's own `venv` and pip; do not install uv or another Python. If Python 3.11+ or its venv support is missing, say exactly what is missing (on Debian or Ubuntu, often `python3-venv`) and link to [Python downloads](https://www.python.org/downloads/). For offline setup, see [prepare the runtime](../../docs/OPERATIONS.md#prepare-the-runtime).

4. **Run setup without a terminal:**

   ```sh
   "<executable>" setup --label "<label>" --project-dir "<project>" --host <host> --prepare-only
   ```

   Add `--state-dir` when the human's bot is registered in a state folder other than the default. Add `--client-file` only to attach an existing client; pass its path and never read the file.

   - **The bot is not registered on this machine yet:** setup reports that. The human must create the bot with `@BotFather` (if they have none), add it to a dedicated group or forum topic, and run the same command **without** `--prepare-only` in their own terminal. The wizard asks for the token in a hidden prompt. Never ask for the token in chat, put it in a tool argument or read it. Direct messages with the bot cannot be paired.
   - **The bot is registered:** the result has a `pairing_command`, such as `/pair@TheBot 1A2B3C4D5E6F`. Give it to the human: they create or pick a dedicated group or topic, add the bot, and post that exact command there. Do not post it anywhere yourself.

5. **Finish the pairing.** When the human says they posted it, run the same `--prepare-only` command again. It collects once and reports `connected: true`. There is no second confirmation: the pairing connects when the person who posted the code owns this bot's other connections, or when the bot has no connection yet. Report the group, topic and owner from the result in one line, so the human can spot a wrong binding.

   If it is still not connected, the result's `next_step` says why. Either the command has not arrived yet (ask the human to check that the bot is in the group, then run setup again), or someone other than the bot's owner posted it. In the second case the human decides in their own terminal with the command that `next_step` gives. Never confirm a pairing for them.

6. **Load the tools.** Follow the result's `next_step` for the host. Claude Code shows the tools in the running session within seconds. Codex, Pi, Amp and Antigravity load the project's MCP entry when the user opens, trusts or reloads the project; do not change trust settings for them. Until the tools load, the same tools run from a shell in the project folder: `"<executable>" tool connection_status` (and `tool messages_poll`, `tool message_send`, `tool messages_ack`), with JSON arguments on stdin.

7. **Check the connection.** Call `connection_status`. Report the label, the bot, the destination and the state.

8. **Offer two options, once each, and act only on a clear yes:**
   - **Owner notices.** The agent can then send the owner private pings from the bot, for things that need them personally, at most 3 per hour and 10 per day. The owner must send `/start` to the bot in a private chat once. On a yes, run `"<executable>" --state-dir "<state_dir>" delivery-settings --client-file "<client_file>" --owner-notify on`, with the paths from `.aot-profile.json`.
   - **Always-on collector.** Only when the setup result's `always_on_collector` has an `offer`. It is a login service on the human's machine, so it is their call. On a yes, rerun the setup command with `--serve` and report the result in one line.

An existing active receiver for the bot needs a deliberate handoff by its owner. Do not stop it, replace a webhook or start a competing receiver. `--receiver-handoff-confirmed` is the human's statement that there is no other receiver, not a way around uncertainty.

Setup does not authorize Telegram messages or ongoing coordination. For status, pause and resume, recent context or messages the user asked for, use [telegram-coordinate](../telegram-coordinate/SKILL.md).

## Host notes

- **Claude Code.** If the plugin is missing, install it with `claude plugin marketplace add fourlastor/agents-over-telegram` and `claude plugin install agents-over-telegram@agents-over-telegram --scope user`. If this session started without it, the user types `/reload-plugins` once (`/reload-plugins --force` in the terminal CLI). Never ask for a restart. In the desktop app a newly added plugin MCP server connects only in the next session; use the shell tools until then.
- **Codex.** After installing or updating the plugin, run `"<executable>" codex-hooks`. If it reports `needs_approval`, ask the user to approve AOT's hooks: the desktop app's hook review, or `/hooks` in the Codex CLI. Codex asks again after every plugin update. Idle desktop chats need the optional [desktop listener](../../docs/DIRECT_DISPATCH.md); start it only when the user asks to listen.
- **Antigravity.** The user plugin adds the hooks. The `SessionStart` hook gives you a waiter command for this conversation; start it as a background command and restart it after each wake-up, as its notice says. See [Antigravity](../../docs/HOSTS.md#antigravity).
- **Amp.** The user plugin starts only in a workspace that was set up when Amp started. After setup, reload plugins (command palette, `plugins: reload`) or restart Amp.

## Update a project to a newer version

Use this only when the user asks to update AOT.

1. Update the host install the same way it was installed (for example `claude plugin update agents-over-telegram@agents-over-telegram`, `pi update`, `agy plugin install` again from the updated source, or `amp skill add` again).
2. Run the newest `scripts/bootstrap_runtime.py` and take the returned `executable`.
3. Read `state_dir` and `label` from `.aot-profile.json` (never the client file). Run `"<executable>" --state-dir "<state_dir>" setup --label "<label>" --project-dir "<project>" --host <host> --prepare-only`. It moves the project to the new runtime and keeps the client, its pairing and its allowed senders. It must report the project as connected, with no pairing command. If it reports anything else, stop and tell the user.
4. For Codex, check `codex-hooks` as above. Then call `connection_status` and report the version and state.
