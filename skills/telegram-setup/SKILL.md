---
name: telegram-setup
description: Connect this agent or project to Telegram with the Agents over Telegram plugin, reuse the human's existing bot, and guide private local pairing and Codex project setup.
---

Use this skill for “connect this agent to Telegram” and plugin onboarding. The normal plugin provides skills; setup creates one native project `telegram` MCP entry for a dedicated group or forum topic, using the human's bot registered once. Another agent project reuses that bot and gets its own client/destination. Unrelated bots and bridges stay outside the task. Do not configure a global plugin MCP server or add `AOT_*` launcher settings for this workflow.

## Connect the project

If this skill's installed package includes a root `mcp.json` or `.mcp.json`, it is an advanced rendered profile with its connection already configured. Check that package's scoped `connection_status` and report any loading problem. Do not run the project setup wizard or create an additional native MCP entry. Rendered packages intentionally have no onboarding action; the steps below apply to the normal skills package.

1. Identify the intended project directory and logical label from the current task. A project is the scope: all Codex chats in that exact directory share its Telegram identity and permissions. Explain this once; if the user needs separate identities, use separate dedicated project directories. Reuse an existing `.aot-profile.json` in that exact directory; do not search parent projects for a profile or choose an unrelated client by label.
2. Select the runtime without reading credentials. If the exact project already has its native `[mcp_servers.telegram]` entry, prefer that configured `command` and matching profile; ordinary onboarding must not migrate an existing project to a new runtime just because the plugin was updated. Check that the absolute executable is available. If it is missing or conflicts with the profile, report the specific repair needed rather than overwriting configuration or selecting another client.

   For a new project without a configured runtime, locate this installed plugin's bundled `scripts/bootstrap_runtime.py` relative to the skill package. Check for an available Python **3.11+** interpreter, then invoke the script with that interpreter. Do not assume the first `python3` on `PATH` is compatible; check its version and select an installed compatible interpreter if needed. For example:

   ```sh
   "/absolute/path/to/python3.11-or-newer" "/absolute/installed/plugin/scripts/bootstrap_runtime.py"
   ```

   Bootstrap needs existing `uv` for a first installation and returns JSON with `status: "ready"`, `version`, `executable`, `runtime_dir` and `reused`. Use its returned absolute `executable`. It prepares a stable private runtime outside the plugin cache; do not require a second source checkout or assume `${PLUGIN_ROOT}/.venv/bin/aot` exists. If Python or uv is unavailable, give the actionable missing requirement and its official [Python](https://www.python.org/downloads/) or [uv](https://docs.astral.sh/uv/getting-started/installation/) installation link. Do not download an interpreter or run a network installer as an incidental fallback. Use `--offline` only when requested or network access is unavailable; cached dependencies are then required. See [runtime details](../../docs/OPERATIONS.md#prepare-the-runtime) for custom paths and repair.

   Use the selected executable for setup:

   ```sh
   "/absolute/path/returned/as/executable" setup --label Alpha --project-dir "/absolute/path/to/project"
   ```

   Reuse the registered account's actual state directory with `--state-dir` when it differs from the default. To attach an existing authorized client, add its file path with `--client-file`; never read or display that file's capability contents. For an already registered account and authorized local setup, run `--prepare-only` to prepare/reuse project files without collecting or sending. Follow the reported next step and skip pairing when already connected. Otherwise give the same command without `--prepare-only` to the human for interactive pairing. First registration belongs in the human's private terminal, including the receiver-ownership assertion; see [pairing details](../../docs/OPERATIONS.md#pair-the-project).
3. For a first bot, guide the human to create it with `@BotFather` and add it to a dedicated group or forum topic; this build does not pair direct messages. The terminal wizard registers or reuses the account, creates or reuses the client, gives an addressed pairing command, collects after the human sends it, and asks the human to review the actual numeric sender/group/topic before confirming. Token entry is hidden. Never ask for a token in chat/tool arguments, read it into model context, or automate the human's destination confirmation. Reuse already-authorized routine setup; do not add a second permission round for the same local work.
4. The wizard prepares `.aot-profile.json` and a single native `[mcp_servers.telegram]` entry in the project's `.codex/config.toml`, preserving unrelated configuration and rejecting conflicts. This is the intended connection; do not add a rendered plugin server as well. The user adds/opens the intended project in Codex and decides whether to trust it. Do not change global trust settings or accept a host trust prompt on their behalf. Reload/reopen the project chat to load its configuration; do not claim the profile is active before checking its tools.
5. Call `connection_status` through that project's MCP tools. Report the logical label, actual bot, destination, state and any returned `next_action`. If tools are unavailable, use the [local controls](../../docs/OPERATIONS.md#everyday-controls) to diagnose setup. Never substitute another project's server to make the check pass.

An existing active bot receiver requires a deliberate owner-coordinated handoff; do not stop it, replace webhooks, or start a competing consumer as routine onboarding. `--receiver-handoff-confirmed` is the human's assertion that the selected bot has no competing receiver, not a bypass for uncertainty.

Setup alone does not authorize Telegram publications or ongoing agent coordination. For status, pause/resume, recent context or explicitly authorized communication, use [telegram-coordinate](../telegram-coordinate/SKILL.md). Intake is on demand; setup does not install a daemon or start listening. If the user explicitly asks to listen, follow the normal-launch listener in [telegram-coordinate](../telegram-coordinate/SKILL.md) and [desktop listening](../../docs/DIRECT_DISPATCH.md). ChatGPT opens from the app list as usual; do not configure a custom launcher, `CODEX_CLI_PATH`, a WebSocket backend override or a protocol wrapper for ordinary setup or listening. Use a bounded listening period and the exact authorized local chat.
