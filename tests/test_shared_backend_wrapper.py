"""Exercise the desktop bridge with an isolated WebSocket app-server double."""
from __future__ import annotations

import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import time

import pytest

from agents_over_telegram.desktop_bridge import (
    BridgeError, handshake_id, is_stdio_app_server, real_binary, validate_socket_path,
)


FAKE_BACKEND = r'''import json, os, pathlib, sys
from websockets.sync.server import unix_serve
args = sys.argv[1:]
inherited_umask = os.umask(0)
os.umask(inherited_umask)
record = {"args": args, "cwd": os.getcwd(), "app_tools": os.environ.get("CODEX_APP_TOOLS_PIPE_PATH"),
          "preserved": os.environ.get("AOT_TEST_PRESERVED"), "pid": os.getpid(), "umask": inherited_umask}
if "--listen" not in args:
    print(json.dumps(record), flush=True)
    raise SystemExit(0)
pathlib.Path(os.environ["AOT_TEST_RECORD"]).write_text(json.dumps(record))
if os.environ.get("AOT_TEST_MODE") == "early-exit":
    raise SystemExit(9)
endpoint = args[args.index("--listen") + 1].removeprefix("unix://")
published = endpoint
if os.environ.get("AOT_TEST_MODE") == "symlink":
    endpoint += ".actual"
def handler(ws):
    for message in ws:
        value = json.loads(message)
        handshake = os.environ.get("AOT_TEST_HANDSHAKE")
        if value.get("method") == "initialize" and handshake:
            if handshake == "error":
                response = {"id": value["id"], "error": {"code": -32000, "message": "test initialization failure"}}
            elif handshake == "mismatch":
                response = {"id": "another-request", "result": {}}
            elif handshake == "bool-id":
                response = {"id": True, "result": {}}
            elif handshake == "server-request":
                response = {"id": value["id"], "method": "test/server-request", "result": {}}
            ws.send(json.dumps(response))
            continue
        if value.get("method") == "initialized":
            continue
        if value.get("method") == "server-request":
            ws.send(json.dumps({"id": 12, "method": "item/commandExecution/requestApproval", "params": {"test": True}}))
        elif value.get("id") == 12:
            ws.send(json.dumps({"method": "approval-result", "params": value["result"]}))
        else:
            ws.send(json.dumps({"id": value["id"], "result": {"echo": value.get("params")}}))
        if os.environ.get("AOT_TEST_MODE") == "crash":
            os._exit(7)
os.umask(0o077)  # Match Codex's explicit private socket creation.
with unix_serve(handler, endpoint) as server:
    os.umask(inherited_umask)
    if published != endpoint:
        pathlib.Path(published).symlink_to(endpoint)
    print("backend stdout belongs on stderr", flush=True)
    server.serve_forever()
'''


@pytest.fixture
def bridge(tmp_path):
    # Linux sockaddr_un paths are short; pytest's verbose test paths aren't.
    import tempfile
    with tempfile.TemporaryDirectory(prefix="aot-bridge-") as directory:
        root = Path(directory)
        root.chmod(0o700)
        binary = root / "codex"
        binary.write_text(f"#!{sys.executable}\n" + FAKE_BACKEND)
        binary.chmod(0o700)
        env = {
            # The desktop launcher itself sets AOT_DISPATCH_*; never let a
            # process-level test inherit a real bot's state or dispatch mode.
            **{key: value for key, value in os.environ.items() if not key.startswith("AOT_")},
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "AOT_CODEX_REAL_BINARY": str(binary),
            "AOT_CODEX_SOCKET": str(root / "backend.sock"),
            "AOT_TEST_RECORD": str(root / "record.json"),
            "CODEX_APP_TOOLS_PIPE_PATH": "/private/test-app-tools.sock",
            "AOT_TEST_PRESERVED": "literal $() argument; no shell",
        }
        running = []

        def start(*args, **updates):
            process = subprocess.Popen(
                [sys.executable, "-m", "agents_over_telegram.desktop_bridge", *args],
                env={**env, **updates}, cwd=tmp_path,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            running.append(process)
            return process

        yield root, env, start
        for process in running:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=10)


def send(process, value):
    process.stdin.write((json.dumps(value) + "\n").encode())
    process.stdin.flush()


def receive(process, timeout=5):
    ready, _, _ = select.select([process.stdout], [], [], timeout)
    assert ready, "bridge did not forward a response"
    line = process.stdout.readline()
    assert line, process.stderr.read().decode()
    return json.loads(line)


def wait_for_endpoint(root, process):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if (root / "backend.sock").exists():
            return
        assert process.poll() is None, process.stderr.read().decode()
        time.sleep(0.02)
    pytest.fail("backend endpoint wasn't created")


def finish(process, *, terminate=False):
    if terminate:
        process.terminate()
    else:
        process.stdin.close()
        process.stdin = None
    return process.communicate(timeout=10)


@pytest.mark.parametrize("args", [
    ["app-server"],
    ["app-server", "--analytics-default-enabled", "-c", 'mcp_servers.example.command="x"'],
    ["-c", 'model="example"', "app-server", "--strict-config"],
    ["app-server", "--config=features.example=true"],
])
def test_only_normal_app_server_is_wrapped(args):
    assert is_stdio_app_server(args)


@pytest.mark.parametrize("args", [
    ["--version"], ["app-server", "--help"], ["app-server", "daemon", "version"],
    ["app-server", "generate-json-schema", "--out", "/tmp/schema"],
    ["app-server", "proxy", "--sock", "/tmp/control"],
    ["app-server", "--listen", "unix:///tmp/explicit"],
    ["app-server", "--listen=stdio://"], ["app-server", "--stdio"],
    ["exec", "app-server"], ["-c", "app-server", "--version"],
])
def test_other_calls_delegate(args):
    assert not is_stdio_app_server(args)


def test_version_delegates_without_requiring_or_opening_socket(bridge):
    root, env, start = bridge
    process = start("--version", AOT_CODEX_SOCKET="")
    output, error = process.communicate(timeout=5)
    assert process.returncode == 0, error
    assert json.loads(output)["args"] == ["--version"]
    assert not (root / "backend.sock.lock").exists()


def test_preserves_args_environment_cwd_and_both_protocol_directions(bridge, tmp_path):
    root, env, start = bridge
    args = ["-c", 'mcp_servers.test.command="literal $() ;"', "app-server", "--analytics-default-enabled"]
    process = start(*args)
    send(process, {"id": 1, "method": "initialize", "params": {"unicode": "Telegram → Codex"}})
    assert receive(process) == {"id": 1, "result": {"echo": {"unicode": "Telegram → Codex"}}}
    record = json.loads((root / "record.json").read_text())
    expected_umask = os.umask(0)
    os.umask(expected_umask)
    assert record["umask"] == expected_umask
    assert record["args"] == [*args, "--listen", "unix://" + env["AOT_CODEX_SOCKET"]]
    assert record["cwd"] == str(tmp_path)
    assert record["app_tools"] == env["CODEX_APP_TOOLS_PIPE_PATH"]
    assert record["preserved"] == env["AOT_TEST_PRESERVED"]
    # Approval requests and responses are forwarded untouched, not auto-approved.
    send(process, {"id": 2, "method": "server-request"})
    assert receive(process) == {"id": 12, "method": "item/commandExecution/requestApproval", "params": {"test": True}}
    send(process, {"id": 12, "result": {"decision": "decline"}})
    assert receive(process) == {"method": "approval-result", "params": {"decision": "decline"}}
    output, error = finish(process)
    assert process.returncode == 0, error
    assert output == b""
    assert b"backend stdout belongs on stderr" in error
    assert not (root / "backend.sock").exists()
    with pytest.raises(ProcessLookupError):
        os.kill(record["pid"], 0)


def test_second_bridge_refuses_same_endpoint_without_disrupting_first(bridge):
    root, env, start = bridge
    first = start("app-server")
    send(first, {"id": 1, "method": "initialize"})
    assert receive(first)["id"] == 1
    second = start("app-server")
    output, error = second.communicate(timeout=5)
    assert second.returncode == 2
    assert b"already owns" in error and output == b""
    send(first, {"id": 2, "method": "ping"})
    assert receive(first)["id"] == 2
    finish(first)


def test_codex_owned_endpoint_symlink_connects_and_only_link_is_cleaned(bridge):
    root, env, start = bridge
    process = start("app-server", AOT_TEST_MODE="symlink")
    send(process, {"id": 1, "method": "initialize"})
    assert receive(process)["id"] == 1
    assert (root / "backend.sock").is_symlink()
    output, error = finish(process)
    assert process.returncode == 0, error
    assert not (root / "backend.sock").is_symlink()
    # The backend owns its real socket; the wrapper only removes its link.
    assert (root / "backend.sock.actual").exists()


def test_idle_desktop_connection_outlives_socket_connect_timeout(bridge):
    root, env, start = bridge
    process = start("app-server")
    send(process, {"id": 1, "method": "initialize"})
    assert receive(process)["id"] == 1
    time.sleep(5.2)
    assert process.poll() is None
    send(process, {"id": 2, "method": "after-idle"})
    assert receive(process)["id"] == 2
    output, error = finish(process)
    assert process.returncode == 0, error


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_signal_stops_owned_backend_and_removes_owned_socket(bridge, number):
    root, env, start = bridge
    process = start("app-server")
    send(process, {"id": 1, "method": "initialize"})
    receive(process)
    record = json.loads((root / "record.json").read_text())
    process.send_signal(number)
    output, error = process.communicate(timeout=10)
    assert process.returncode == 128 + number, error
    assert output == b""
    assert not (root / "backend.sock").exists()
    with pytest.raises(ProcessLookupError):
        os.kill(record["pid"], 0)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux parent-death signaling")
def test_killed_wrapper_does_not_leave_backend_running(bridge):
    root, env, start = bridge
    process = start("app-server")
    send(process, {"id": 1, "method": "initialize"})
    receive(process)
    pid = json.loads((root / "record.json").read_text())["pid"]
    process.kill()
    process.communicate(timeout=5)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            status = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()[0]
        except FileNotFoundError:
            break
        if status == "Z":  # Exited and waiting for the host init process to reap.
            break
        time.sleep(0.02)
    else:
        pytest.fail("backend survived the wrapper's SIGKILL")
    # SIGKILL cannot run cleanup. A new owner refuses the stale endpoint.
    assert (root / "backend.sock").exists()
    retry = start("app-server")
    output, error = retry.communicate(timeout=5)
    assert retry.returncode == 2 and b"already exists" in error


def test_backend_crash_is_reported_and_endpoint_cleaned(bridge):
    root, env, start = bridge
    process = start("app-server", AOT_TEST_MODE="crash")
    send(process, {"id": 1, "method": "initialize"})
    process.wait(timeout=10)
    output, error = process.communicate(timeout=10)
    assert process.returncode == 2
    assert b"exited" in error or b"closed unexpectedly" in error
    assert not (root / "backend.sock").exists()


def test_backend_start_failure_has_no_protocol_stdout(bridge):
    root, env, start = bridge
    process = start("app-server", AOT_TEST_MODE="early-exit")
    process.wait(timeout=5)
    output, error = process.communicate(timeout=5)
    assert process.returncode == 2
    assert output == b"" and b"exited before" in error
    assert not (root / "backend.sock").exists()


def test_existing_socket_is_never_removed_or_reused(bridge):
    root, env, start = bridge
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as other:
        other.bind(env["AOT_CODEX_SOCKET"])
        before = Path(env["AOT_CODEX_SOCKET"]).stat()
        process = start("app-server")
        output, error = process.communicate(timeout=5)
        assert process.returncode == 2
        assert b"already exists" in error and output == b""
        assert Path(env["AOT_CODEX_SOCKET"]).stat().st_ino == before.st_ino
        assert not (root / "record.json").exists()


def test_socket_cleanup_does_not_unlink_replacement(bridge):
    root, env, start = bridge
    process = start("app-server")
    send(process, {"id": 1, "method": "initialize"})
    receive(process)
    path = Path(env["AOT_CODEX_SOCKET"])
    path.unlink()
    path.write_text("replacement belongs to someone else")
    finish(process)
    assert path.read_text() == "replacement belongs to someone else"


@pytest.mark.parametrize("kind", ["parent-symlink", "socket-symlink", "insecure-parent", "lock-symlink", "lock-hardlink"])
def test_rejects_unsafe_paths_without_mutating_targets(bridge, kind):
    root, env, start = bridge
    target = root / "preserve"
    target.write_text("unchanged")
    path = Path(env["AOT_CODEX_SOCKET"])
    if kind == "parent-symlink":
        (root / "alias").symlink_to(root, target_is_directory=True)
        path = root / "alias" / "backend.sock"
    elif kind == "socket-symlink":
        path.symlink_to(target)
    elif kind == "insecure-parent":
        root.chmod(0o755)
    elif kind == "lock-symlink":
        path.with_name(path.name + ".lock").symlink_to(target)
    elif kind == "lock-hardlink":
        target.chmod(0o600)
        os.link(target, path.with_name(path.name + ".lock"))
    process = start("app-server", AOT_CODEX_SOCKET=str(path))
    output, error = process.communicate(timeout=5)
    assert process.returncode == 2 and output == b""
    assert target.read_text() == "unchanged"
    assert not (root / "record.json").exists()


@pytest.mark.parametrize("value", ["", "relative.sock", "/tmp/../tmp/test.sock", "/" + "x" * 108, "/tmp/\x00sock"])
def test_rejects_ambiguous_or_unusable_socket_names(value):
    with pytest.raises(BridgeError):
        validate_socket_path(value)


def test_requires_explicit_real_binary():
    for value in ("", "codex", "/does/not/exist"):
        with pytest.raises(BridgeError):
            real_binary({"AOT_CODEX_REAL_BINARY": value})


def dispatch_environment(root):
    """Use the real bridge with an isolated dispatcher process double."""
    package = root / "shadow" / "agents_over_telegram"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "desktop_bridge.py").symlink_to(
        Path(__file__).resolve().parents[1] / "src/agents_over_telegram/desktop_bridge.py"
    )
    (package / "cli.py").write_text('''import json, os, pathlib, sys, time
record = {"args": sys.argv[1:], "pid": os.getpid(), "cwd": os.getcwd()}
pathlib.Path(os.environ["AOT_TEST_DISPATCH_RECORD"]).write_text(json.dumps(record))
print(json.dumps({"type": "test_dispatch_ready"}), flush=True)
if os.environ.get("AOT_TEST_DISPATCH_MODE") == "exit":
    raise SystemExit(7)
if os.environ.get("AOT_TEST_DISPATCH_MODE") == "log":
    for _ in range(2000):
        print(json.dumps({"type": "test_log", "content": "x" * 1000}), flush=True)
while True:
    time.sleep(1)
''')
    (root / "state").mkdir()
    return {
        "PYTHONPATH": str(root / "shadow"),
        "AOT_DISPATCH_STATE": str(root / "state"),
        "AOT_TEST_DISPATCH_RECORD": str(root / "dispatcher-record.json"),
    }


def wait_for_file(path):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size:
            return
        time.sleep(0.02)
    pytest.fail("expected child process metadata wasn't written")


def test_opt_in_dispatcher_starts_after_successful_initialize_without_notification(bridge):
    root, env, start = bridge
    options = dispatch_environment(root)
    process = start("app-server", **options, AOT_DISPATCH_FAKE="1", AOT_DISPATCH_COLLECT="1")
    send(process, {"id": 0, "method": "ping-before-initialize"})
    receive(process)
    time.sleep(0.15)
    assert not (root / "dispatcher-record.json").exists()
    send(process, {"id": "__codex_initialize__", "method": "initialize"})
    assert receive(process)["id"] == "__codex_initialize__"
    wait_for_file(root / "dispatcher-record.json")
    record = json.loads((root / "dispatcher-record.json").read_text())
    assert record["cwd"] == str(root)
    assert record["args"] == [
        "--state-dir", str(root / "state"), "--fake", "relay-dispatch",
        "--endpoint", "unix://" + env["AOT_CODEX_SOCKET"], "--duration", "0", "--collect",
    ]
    output, error = finish(process)
    assert process.returncode == 0, error
    assert output == b""
    assert (root / "dispatcher.jsonl").stat().st_mode & 0o777 == 0o600
    assert "test_dispatch_ready" in (root / "dispatcher.jsonl").read_text()
    with pytest.raises(ProcessLookupError):
        os.kill(record["pid"], 0)
    assert not (root / "backend.sock").exists()


def test_legacy_initialized_notification_starts_dispatcher_only_once(bridge):
    root, env, start = bridge
    process = start("app-server", **dispatch_environment(root))
    send(process, {"id": 0, "method": "ping-before-initialize"})
    receive(process)
    send(process, {"method": "initialized"})
    wait_for_file(root / "dispatcher-record.json")
    original_record = (root / "dispatcher-record.json").read_bytes()
    send(process, {"id": 1, "method": "initialize"})
    receive(process)
    send(process, {"method": "initialized"})
    time.sleep(0.15)
    assert (root / "dispatcher-record.json").read_bytes() == original_record
    output, error = finish(process)
    assert process.returncode == 0, error


@pytest.mark.parametrize("mode", ["error", "mismatch", "bool-id", "server-request"])
def test_only_matching_successful_initialize_response_starts_dispatcher(bridge, mode):
    root, env, start = bridge
    process = start("app-server", **dispatch_environment(root), AOT_TEST_HANDSHAKE=mode)
    send(process, {"id": 1, "method": "initialize"})
    receive(process)
    time.sleep(0.15)
    assert not (root / "dispatcher-record.json").exists()
    assert not (root / "dispatcher.jsonl").exists()
    send(process, {"id": 3, "method": "still-working"})
    assert receive(process)["id"] == 3
    output, error = finish(process)
    assert process.returncode == 0, error


@pytest.mark.parametrize("value", [None, True, False, [], {}, 1.0, 1 << 63, -(1 << 63) - 1, "x" * 257])
def test_initialize_id_must_be_bounded_string_or_integer(value):
    assert handshake_id(value) is None


def test_initialize_id_comparison_preserves_json_type():
    assert handshake_id(1) == ("integer", 1)
    assert handshake_id("1") == ("string", "1")
    assert handshake_id(1) != handshake_id("1")
    assert handshake_id("__codex_initialize__") == ("string", "__codex_initialize__")


def test_unexpected_dispatcher_exit_preserves_desktop_and_does_not_restart(bridge):
    root, env, start = bridge
    process = start("app-server", **dispatch_environment(root), AOT_TEST_DISPATCH_MODE="exit")
    send(process, {"id": 1, "method": "initialize"})
    receive(process)
    send(process, {"method": "initialized"})
    wait_for_file(root / "dispatcher-record.json")
    original_record = (root / "dispatcher-record.json").read_bytes()
    deadline = time.monotonic() + 5
    while "dispatcher_error" not in (root / "dispatcher.jsonl").read_text():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert process.poll() is None
    send(process, {"id": 2, "method": "still-working"})
    assert receive(process)["id"] == 2
    time.sleep(0.15)
    assert (root / "dispatcher-record.json").read_bytes() == original_record
    output, error = finish(process)
    assert process.returncode == 0, error
    assert b"dispatcher exited" in error
    assert not (root / "backend.sock").exists()
    pid = json.loads((root / "record.json").read_text())["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_dispatcher_is_never_implicitly_started(bridge):
    root, env, start = bridge
    process = start("app-server")
    send(process, {"id": 1, "method": "initialize"})
    receive(process)
    send(process, {"method": "initialized"})
    time.sleep(0.15)
    finish(process)
    assert not (root / "dispatcher.jsonl").exists()


@pytest.mark.parametrize("updates", [
    {"AOT_DISPATCH_COLLECT": "1"},
    {"AOT_DISPATCH_STATE": "relative"},
    {"AOT_DISPATCH_FAKE": "1"},
    {"AOT_DISPATCH_STATE": "/tmp", "AOT_DISPATCH_COLLECT": "true"},
])
def test_invalid_dispatcher_configuration_fails_before_starting_backend(bridge, updates):
    root, env, start = bridge
    process = start("app-server", **updates)
    output, error = process.communicate(timeout=5)
    assert process.returncode == 2 and output == b""
    assert not (root / "record.json").exists()


def test_dispatcher_log_is_bounded_and_symlink_is_never_followed(bridge):
    root, env, start = bridge
    process = start("app-server", **dispatch_environment(root), AOT_TEST_DISPATCH_MODE="log")
    send(process, {"id": 1, "method": "initialize"})
    receive(process)
    send(process, {"method": "initialized"})
    wait_for_file(root / "dispatcher.jsonl")
    time.sleep(0.2)
    finish(process)
    assert 0 < (root / "dispatcher.jsonl").stat().st_size <= 1024 * 1024
    assert all(isinstance(json.loads(line), dict) for line in (root / "dispatcher.jsonl").read_text().splitlines())
    target = root / "preserved-log"
    target.write_text("unchanged")
    (root / "dispatcher.jsonl").unlink()
    (root / "dispatcher.jsonl").symlink_to(target)
    process = start("app-server", **{
        "PYTHONPATH": str(root / "shadow"),
        "AOT_DISPATCH_STATE": str(root / "state"),
        "AOT_TEST_DISPATCH_RECORD": str(root / "dispatcher-record.json"),
    })
    send(process, {"id": 1, "method": "initialize"})
    receive(process)
    send(process, {"method": "initialized"})
    time.sleep(0.15)
    send(process, {"id": 2, "method": "still-working"})
    assert receive(process)["id"] == 2
    output, error = finish(process)
    assert process.returncode == 0
    assert b"dispatcher could not start" in error
    assert target.read_text() == "unchanged"
