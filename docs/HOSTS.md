# Agent hosts and push delivery

Agents over Telegram (AOT) works with four agent hosts: **Codex**, **Claude Code**, **Pi** and **Amp**. Each host uses its own plugin or extension system. In every host, a new Telegram message reaches the running agent without anyone typing a prompt. An idle agent wakes up. A busy agent gets the message at its next safe point, usually after the current tool call.

This page explains how each integration works, how to set it up, and what was tested. The [evidence section](#what-was-tested) says exactly which checks used a real agent host and which were only offline tests.

## How push works

All hosts share the same parts:

- **The inbox and tools stay the same.** Each project has its own client file and inbox. The agent still reads messages with `messages_poll`, replies with `message_send` and confirms with `messages_ack`.
- **A notice, never the message.** The push is a fixed notice such as "1 new Telegram message for this agent. From: @alice (human)." It never contains the message text. The agent must poll to read the text, so leases, acknowledgements, pause and sender rules still apply.
- **One notice per message.** AOT records which deliveries were announced. A watcher and a hook never announce the same message twice.
- **Collection.** Something must fetch updates from Telegram. In Claude Code, Pi and Amp, a small watcher (`aot watch --collect`) runs while the host is open. It long-polls Telegram and announces new messages. In Codex, the hooks collect at most every 10 seconds, or the `relay-desktop` listener collects.
- **Per-project runtime.** Setup writes `.aot-runtime.json` next to `.aot-profile.json`. It names the prepared `aot` executable for that project and contains no secret. Host plugins read this file only in the exact project folder. They never search parent folders and never use an `aot` found on `PATH`.
- **Unrelated projects stay quiet.** In a folder without AOT setup, the plugins do nothing.
- **No orphan processes.** The watcher stops when the host process that started it exits, even after a forced kill.

| Host | Plugin type | Idle agent | Busy agent | Tools come from |
|---|---|---|---|---|
| Claude Code | Claude Code plugin (`.claude-plugin/`) | A plugin **monitor** prints the notice; Claude Code wakes the session | The monitor notice, or a `PostToolUse` hook, arrives after the current tool call; a `Stop` hook catches messages that arrive during the final answer | The plugin's MCP server |
| Codex | Codex plugin hooks (`codex/hooks.json`) | The existing `relay-desktop` listener wakes a desktop chat ([details](DIRECT_DISPATCH.md)) | `PostToolUse` and `Stop` hooks add the notice | `.codex/config.toml` written by setup |
| Pi | Pi extension (`integrations/pi/`) | The extension calls `pi.sendMessage(..., {triggerTurn: true})` and Pi starts a turn | Pi delivers the same message as a steering message after the current tool call | `.pi/mcp.json` written by setup |
| Amp | Amp plugin (`integrations/amp/`) | The plugin appends the notice to the current thread; it opens a thread if none exists | `appendUserMessage(..., {steer: true})` queues it as a steering message | `.amp/settings.json` written by setup |

## Set up a project

Register the bot and pair the project once, as described in [local setup](OPERATIONS.md#codex-setup). Choose the host with `--host`:

```sh
aot setup --label Alpha --project-dir /absolute/path/to/project --host claude
```

`--host` accepts `codex` (the default), `claude`, `pi` and `amp`. Setup always writes `.aot-profile.json` and `.aot-runtime.json`. It also writes the host's MCP configuration: `.codex/config.toml` for Codex, `.pi/mcp.json` for Pi and `.amp/settings.json` for Amp. Claude Code gets its MCP server from the plugin. Setup keeps unrelated settings and refuses to replace a different `telegram` entry. You can run setup again with another `--host` to use the same project from a second host.

### Claude Code

1. Add the marketplace and install the plugin **for this project only** (run this inside the project folder):

   ```sh
   claude plugin marketplace add fourlastor/agents-over-telegram
   claude plugin install agents-over-telegram@agents-over-telegram --scope local
   ```

   Local scope keeps the plugin out of other projects. In other projects it would stay quiet anyway, but the MCP server would report that the folder is not set up.
2. Start Claude Code in the project folder and ask "Telegram status".
3. Keep the session open. The monitor runs only in **interactive** sessions. It also collects Telegram updates for this project, so you do not need a separate listener.

The plugin needs `python3` 3.8 or newer on `PATH` to run the small launcher. The launcher only reads `.aot-runtime.json` and then starts the prepared runtime, which uses its own Python.

Claude Code also offers "channels" for chat bridges. Channels need an allowlisted plugin or the `--dangerously-load-development-channels` flag, and they do not work with `-p`. AOT uses a monitor and hooks instead, which need neither.

In `-p` (non-interactive) mode, monitors do not start. The hooks still add notices at tool boundaries, but only if something else collects from Telegram, for example `aot watch --collect` or `relay-desktop --collect`.

### Codex

The Codex plugin now ships hooks for `SessionStart`, `UserPromptSubmit`, `PostToolUse` and `Stop`. Codex asks you to review and trust plugin hooks once. Open the Codex CLI in the project and run `/hooks` to review them. The desktop app may not show `/hooks`.

The hooks use the project folder from Codex's hook input (`cwd`). They collect from Telegram at most every 10 seconds. If `relay-desktop --collect` is running, the hooks see its collector lock and do not compete with it. Idle desktop chats still need `relay-desktop`, as before ([listening](DIRECT_DISPATCH.md)).

Setup also approves three tools of the project's scoped `telegram` server in `.codex/config.toml`: `messages_poll`, `message_send` and `messages_ack`. Without this, Codex's automatic approval reviewer (`approvals_reviewer = "auto_review"`) rejected `messages_poll` in a live run. It judged the call as drift from the user's typed task, because the request came from a hook notice. The approval covers only these three tools of this project's server. That server can read only this project's inbox and send only to its bound topic, within the existing rate and exchange limits. Pause, disconnect and connection tools still follow your normal approval policy. If the config already has a `tools` table for `telegram`, setup leaves it unchanged. Remove the three tables to require approval again.

### Pi

1. Run setup with `--host pi`. It writes `.pi/mcp.json` with `exposure: direct`, so the tools appear as `telegram/messages_poll` and so on.
2. Load the extension. For one run: `pi -e /path/to/agents-over-telegram/integrations/pi/agents-over-telegram.ts`. To keep it for the project: `pi install /path/to/agents-over-telegram/integrations/pi -l`.
3. Start Pi in the project folder and trust the project when Pi asks. Pi ignores `.pi/mcp.json` in untrusted projects.

### Amp

1. Run setup with `--host amp`. It writes `amp.mcpServers.telegram` to `.amp/settings.json`.
2. Approve the workspace server once: `amp mcp approve telegram`.
3. Copy or link `integrations/amp/agents-over-telegram.ts` into the project's `.amp/plugins/` folder. Amp loads project plugins from there. Reload with the command palette action `plugins: reload`, or restart Amp.

## What was tested

All checks below ran on macOS between 2026-11-02 and 2026-11-03 with the **fake Telegram transport**. That transport is a local JSON file: the test adds a Telegram update to it and reads back what the agent published. No bot token and no Telegram network were used. The real bots on this machine were not touched. Each run used a new isolated state folder.

`scripts/e2e_fake.py` prepares such a folder and injects messages. Each check below ran the **real host** with the **real integration** loaded, and the agent handled the message without any typed follow-up. "Pass" means the fake Telegram recorded the reply and the inbox showed zero pending messages afterwards.

| Host and version | Model | Scenario | Result |
|---|---|---|---|
| Claude Code 2.1.302, interactive, plugin loaded with `--plugin-dir` | Sonnet 5.5 | Idle session at the prompt | Pass. Reply 10 seconds after the message (earlier runs: 5–7 seconds, 3 of 3 messages). |
| Claude Code 2.1.302, interactive | Sonnet 5.5 | Busy: three `sleep 20` commands; message sent during the first | Pass after a fix (see below). Reply 16 seconds after the message, right after the first sleep. The task then continued. |
| Claude Code 2.1.302, `-p` (hooks only, separate collector loop) | Sonnet 5.5 | Busy: three `sleep 15` commands | Pass after the same fix. The `PostToolUse` hook notice arrived after the first sleep; reply 21 seconds after the message. |
| Codex CLI 0.163.0, `codex exec`, plugin hooks file loaded as a project hook (see below), hook trust bypassed for this one run | Codex default (`gpt-6.1-terra`) | Busy: three `sleep 15` commands; message sent during the first | Pass. The `PostToolUse` hook notice arrived after the first sleep. Codex polled, replied and acknowledged, then ran the other two sleeps. Reply 55 seconds after the message. |
| Pi 1.5.0, interactive, `-e` extension, `.pi/mcp.json` | `openai-codex/gpt-6.1-terra` | Idle | Pass. The extension started a turn; reply 27 seconds after the message. |
| Pi 1.5.0, interactive | `openai-codex/gpt-6.1-terra` | Busy: three `sleep 15` commands | Pass. Pi steered the notice in after the first sleep; reply 24 seconds after the message. The agent acknowledged it and finished the remaining sleeps. |
| Pi 1.5.0, interactive | Scripted local model (earlier run) | Idle and busy | Pass. Replies 1 and 4 seconds after the message. |
| Amp 0.0.1793649628, interactive TUI, project plugin, MCP via `--mcp-config` | Amp default ("medium") | Idle, newly opened Amp with no thread | Pass. The plugin opened a thread. Reply 18 seconds after the message. |
| Amp 0.0.1793649628, interactive TUI | Amp default | Busy: three `sleep 15` commands | Pass. The steering notice appeared after the current sleep. The agent replied (26 seconds after the message; 78 seconds in an earlier run) and acknowledged before the task ended. |
| Amp 0.0.1793649628 | — | Amp killed with `SIGKILL` | Pass. The watcher exited by itself within 15 seconds. |
| All three hosts | — | Host session closed after the checks | Pass. No AOT watcher or MCP process was left running. |

The Claude Code and Amp interactive rows come from a final run of the hosts at the same time on the documented code. The `-p` row and the `SIGKILL` row come from earlier runs on the same day, before the documentation changes. The Codex row and the Pi rows with a real model come from a later run on 2026-11-03, after the Codex tool approvals were added. In every one of these rows the fake Telegram recorded exactly one reply per message, and the inbox had zero pending messages afterwards.

**Fixes found by these runs.** The first busy Claude Code run delivered the notice mid-task, but the model said a Telegram message was waiting and asked the user whether to handle it. The notice and the MCP server instructions now say that the local user wants these messages handled when they arrive, without asking first. They also still say that message text cannot widen permissions, approvals or recipients. After this change, both Claude Code busy checks passed. The runs also found a monitor that stopped in an unconfigured folder (Claude Code then reports the stop as an event) and a watcher left running after its host was killed. Both are fixed. The first live Codex run found that Codex's automatic approval reviewer rejects `messages_poll` when a hook notice asks for it. Setup now approves the three push-handling tools (see [Codex](#codex)).

**How the Codex check loaded the hooks.** Installing the plugin from a marketplace would change the user's Codex configuration, so the check did not do it. Instead it copied the plugin's `codex/hooks.json` unchanged into the test project's `.codex/hooks.json` and set `PLUGIN_ROOT` to this checkout. The hook commands, the launcher and the runtime were the same as in the plugin. The project layer had to be trusted, and `--dangerously-bypass-hook-trust` replaced the `/hooks` review for this one run. Codex then stored a trust entry for each test folder in the user's `~/.codex/config.toml`; those entries were removed afterwards.

**Pi with a real model needs Node.js network access.** In the first Pi runs, Node.js could not reach the model providers, so the check used `scripts/e2e/scripted_model.py`, a small OpenAI-compatible model on `127.0.0.1`. The later run used a real OpenAI model. On the test machine, Node.js could still not reach Anthropic hosts (a raw TCP connection from Node.js timed out while `curl` and Python connected), so Pi's default Claude model failed there. This is a local network filter, not part of AOT.

**Not tested end to end:**

- **Real Telegram** for any of these new integrations. Earlier live Telegram evidence for Codex `relay-desktop` is in [compatibility](COMPATIBILITY.md).
- **Codex plugin hooks after a marketplace install and a `/hooks` review.** The live check used the same hooks file as a project hook, with trust bypassed (see above). The Codex desktop app and the interactive Codex CLI were not tested; the check used `codex exec`. The `Stop` continuation was covered only offline, in `tests/test_codex_hooks.py`.
- **Installation from a marketplace** for Claude Code and Codex, and `pi install` for Pi. The checks loaded the plugin with `--plugin-dir` (Claude Code), `-e` (Pi) and a copy in `.amp/plugins/` (Amp).
- **The Amp workspace approval flow.** The check passed the same server entry with `--mcp-config` instead of approving `.amp/settings.json`.
- **The Claude Code desktop app** (Code tab). Its documentation does not say whether plugin monitors run there.
- **Linux** for the new integrations.

## Security notes

- Push notices contain only counts and fixed-format sender labels: `@username (human)`, `@username (bot)`, `user 123 (human)` or `a peer agent`. Usernames must match Telegram's character set; anything else falls back to the numeric ID.
- Setup approves only `messages_poll`, `message_send` and `messages_ack` on the project's scoped Codex server, so Codex's approval reviewer does not block pushed work. Hooks never send, lease or acknowledge. They exit 0 with no output on any error, so a broken setup cannot block the host. The Codex hook may collect from Telegram, at most every 10 seconds.
- A `Stop` hook continues a turn at most once in a row (`stop_hook_active`), so it cannot loop.
- Pi and Amp receive the notice as a user message from the plugin. Claude Code receives it as a monitor event or as hook context. In all cases the text is generated by AOT, not taken from Telegram.
- The launcher and the Pi and Amp plugins accept `.aot-runtime.json` only if it is a regular file owned by the user and not writable by group or others. The same applies to `.aot-profile.json`.
