# Continuous listening on macOS

Use this procedure only when the operator asks for a permanent listener or automatic restart after login. Ordinary setup remains on demand.

## Prepare the service

1. Verify the existing target UUID, exact project, private state and configured runtime with `relay-status`. Keep the existing client and pairing.
2. Stop any temporary listener for that state. Run only one notification driver. Do not start another `getUpdates` consumer.
3. Use `relay-desktop --duration 0 --collect` for indefinite listening. Keep the stable absolute runtime path outside the plugin cache.
4. Check existing power management with the operator. Reuse tools such as Amphetamine when already configured. Do not add `caffeinate` or change sleep settings as an incidental service dependency.

A LaunchAgent runs after the user logs in. It does not run before login, unlock FileVault, or start the desktop app. The listener waits when the desktop is absent. The Mac must remain awake and online, and the normal Codex desktop must be open for target wakeups.

## LaunchAgent template

Replace every `REPLACE_...` value with an absolute local path. Plist strings do not expand `$HOME` or `~`. Use the existing runtime and canonical account state. Do not put a token or client capability in the plist.

Save this file as `~/Library/LaunchAgents/local.agents-over-telegram.plist`. Preserve an existing service instead of overwriting it. Create the private log directory first with mode `0700`; create both log files and the plist with mode `0600`.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>local.agents-over-telegram</string>
  <key>ProgramArguments</key>
  <array>
    <string>REPLACE_ABSOLUTE_AOT_EXECUTABLE</string>
    <string>--state-dir</string>
    <string>REPLACE_ABSOLUTE_ACCOUNT_STATE</string>
    <string>relay-desktop</string>
    <string>--duration</string>
    <string>0</string>
    <string>--collect</string>
  </array>
  <key>WorkingDirectory</key>
  <string>REPLACE_EXACT_PROJECT_DIRECTORY</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>REPLACE_USER_HOME</string>
    <key>PATH</key>
    <string>/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>30</integer>
  <key>ExitTimeOut</key>
  <integer>20</integer>
  <key>ProcessType</key>
  <string>Background</string>
  <key>StandardOutPath</key>
  <string>REPLACE_PRIVATE_LOG_DIRECTORY/listener.stdout.log</string>
  <key>StandardErrorPath</key>
  <string>REPLACE_PRIVATE_LOG_DIRECTORY/listener.stderr.log</string>
</dict>
</plist>
```

For a nondefault Codex home, pass the matching `--codex-home` explicitly in `ProgramArguments`. Do not rely on an interactive shell environment. Reuse the operator's log retention policy; launchd does not rotate these files for this template.

The following controls use **fish** syntax. Replace the plist label if an existing service uses another name.

```fish
set --local aot_uid (id -u)
plutil -lint "$HOME/Library/LaunchAgents/local.agents-over-telegram.plist"
launchctl bootstrap gui/$aot_uid "$HOME/Library/LaunchAgents/local.agents-over-telegram.plist"
launchctl print gui/$aot_uid/local.agents-over-telegram
```

Verify `state = running` and a fresh `desktop_dispatcher_ready` event with `duration: 0` and `collect: true`. A successful bootstrap or PID alone is insufficient. Inspect errors before attempting another start.

To stop the service, use `launchctl bootout gui/$aot_uid/local.agents-over-telegram`. Sending SIGTERM alone lets `KeepAlive` restart it. Remove or archive the plist only when the operator also wants to remove login startup; preserve account state and delivery records.

## Acceptance and handoff

- Send a fresh authorized Telegram marker while the target is idle. Verify the target's own scoped poll, one sent reply, and acknowledgment.
- Test a busy target separately. The native queue can wait until the current turn ends; see [busy sessions and hooks](DIRECT_DISPATCH.md#busy-sessions-and-hooks).
- With no send in flight, stop the listener gracefully and verify launchd starts it again. Confirm retained messages and no duplicate notification. This checks service recovery, not recovery from an OS reboot.
- Reopen the desktop and verify the same target, tools and runtime still work. A configured login service does not prove a reboot test passed.
- Report remaining pending or uncertain work, active sender permissions, whether listening is continuous, and any untested recovery boundary. Do not claim unattended operation solely because the plist exists.

A local macOS deployment completed idle and busy-target Telegram checks, desktop reopen, and graceful-stop recovery. The busy test used an additional operator-installed hook. The plugin does not install that hook or this service automatically.
