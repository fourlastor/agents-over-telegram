import contextlib
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve, unix_serve

from agents_over_telegram import appserver_client
from agents_over_telegram.appserver_client import AppServerConnection, AppServerError, _check_peer, validate_endpoint
from agents_over_telegram.credentials import write_private_json


SECRET = "SERVER_SECRET_SHOULD_NOT_ESCAPE_12345"
THREAD = "01a15453-3da7-7e30-af14-c701e241fed0"
TURN = "01a15453-3dc7-73e3-b263-0dc1d8de7afd"


def send(ws, value):
    ws.send(json.dumps(value))


@contextlib.contextmanager
def fake_server(handler, *, tcp=False):
    """Real local socket/HTTP/WebSocket/JSON-RPC with isolated generated data."""
    errors, received = [], []

    def run(ws):
        try:
            initialization = json.loads(ws.recv(timeout=2))
            received.append(initialization)
            assert initialization["method"] == "initialize"
            send(ws, {"id": initialization["id"], "result": {"userAgent": "test"}})
            received.append(json.loads(ws.recv(timeout=2)))
            assert received[-1] == {"method": "initialized"}
            handler(ws, received)
        except ConnectionClosed:
            pass
        except BaseException as error:
            errors.append(error)

    with tempfile.TemporaryDirectory(prefix="aot-ws-") as directory:
        path = Path(directory).resolve() / "rpc.sock"
        with (serve(run, "127.0.0.1", 0, close_timeout=0.1) if tcp else
              unix_serve(run, str(path), close_timeout=0.1)) as server:
            if tcp:
                endpoint = f"ws://127.0.0.1:{server.socket.getsockname()[1]}/rpc"
            else:
                path.chmod(0o600)
                endpoint = "unix://" + str(path)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield endpoint, received
            finally:
                server.shutdown()
                thread.join(timeout=2)
        if errors:
            raise errors[0]


def echo(ws, received):
    for raw in ws:
        request = json.loads(raw)
        received.append(request)
        send(ws, {"id": request["id"], "result": {"ok": True}})


def test_real_unix_handshake_and_serial_requests():
    with fake_server(echo) as (endpoint, received):
        with AppServerConnection(endpoint) as client:
            assert client.request("thread/loaded/list", {}) == {"ok": True}
            assert client.request("turn/start", {"threadId": THREAD}, mutating=True) == {"ok": True}
        assert [item["method"] for item in received] == ["initialize", "initialized", "thread/loaded/list", "turn/start"]
        assert received[0]["params"]["capabilities"] == {"experimentalApi": True}
        with pytest.raises(AppServerError, match="already_used"):
            client.connect()


def test_bidirectional_requests_are_left_for_desktop_and_metadata_is_redacted():
    def handler(ws, received):
        request = json.loads(ws.recv())
        send(ws, {"method": "turn/started", "params": {"threadId": THREAD, "turn": {"id": TURN}, "content": SECRET}})
        send(ws, {"method": "item/commandExecution/requestApproval", "id": "approval-id",
                  "params": {"threadId": THREAD, "turnId": TURN, "command": SECRET}})
        send(ws, {"method": SECRET, "params": {"threadId": SECRET, "turnId": SECRET}})
        send(ws, {"method": SECRET, "id": "unknown-request", "params": {"text": SECRET}})
        send(ws, {"id": request["id"], "result": {"ok": True}})
        # The dispatcher must not deny, fulfill, or auto-approve broadcast work.
        with pytest.raises(TimeoutError):
            ws.recv(timeout=0.08)
        request = json.loads(ws.recv(timeout=2))
        received.append(request)
        send(ws, {"id": request["id"], "result": {}})
    with fake_server(handler) as (endpoint, _):
        with AppServerConnection(endpoint) as client:
            client.request("turn/start", {}, mutating=True)
            events = client.drain()
            assert events == [
                {"kind": "notification", "method": "turn/started", "threadId": THREAD, "turnId": TURN},
                {"kind": "server_request", "method": "item/commandExecution/requestApproval", "threadId": THREAD, "turnId": TURN},
                {"kind": "server_request", "method": "other"},
            ]
            assert SECRET not in json.dumps(events)
            assert client.server_requests_received == 2
            assert client.notifications_received == 2
            time.sleep(0.1)
            assert client.request("thread/loaded/list", {}) == {}


def test_busy_turn_deltas_do_not_fill_event_buffer_or_fail_mutation():
    def handler(ws, _):
        for raw in ws:
            request = json.loads(raw)
            for _ in range(600):
                send(ws, {"method": "item/agentMessage/delta", "params": {
                    "threadId": THREAD, "turnId": TURN, "delta": SECRET,
                }})
            send(ws, {"method": "turn/started", "params": {"threadId": THREAD, "turn": {"id": TURN}}})
            send(ws, {"id": request["id"], "result": {"ok": True}})
    with fake_server(handler) as (endpoint, _):
        with AppServerConnection(endpoint, max_events=4, timeout=3) as client:
            for method in ("thread/read", "thread/resume", "turn/start"):
                assert client.request(method, {}, mutating=method == "turn/start") == {"ok": True}
            events = client.drain()
            assert len(events) == 3
            assert all(event["method"] == "turn/started" for event in events)
            assert SECRET not in json.dumps(events)
            assert client.notifications_received == 1803


def test_drain_bounds_frames_even_when_stream_is_continuously_discarded():
    class ContinuousDeltas:
        def __init__(self):
            self.read = 0
        def recv(self, timeout):
            self.read += 1
            return json.dumps({"method": "item/agentMessage/delta", "params": {"delta": SECRET}})
        def close(self):
            pass
    stream = ContinuousDeltas()
    client = AppServerConnection("unix:///tmp/private/rpc.sock", max_events=4)
    client._socket = stream
    assert client.drain() == []
    assert stream.read == 4
    assert client.notifications_received == 4
    assert client.drain() == []
    assert stream.read == 8


def test_malformed_discardable_notification_still_fails_validation():
    def handler(ws, _):
        request = json.loads(ws.recv())
        send(ws, {"method": "item/agentMessage/delta", "result": {"secret": SECRET}})
        send(ws, {"id": request["id"], "result": {}})
    with fake_server(handler) as (endpoint, _):
        with AppServerConnection(endpoint) as client:
            with pytest.raises(AppServerError) as caught:
                client.request("turn/start", {}, mutating=True)
            assert caught.value.ambiguous is True
            assert caught.value.code == "invalid_appserver_response"


def test_persistent_drain_waits_for_future_notifications_without_sending():
    def handler(ws, _):
        time.sleep(0.02)
        send(ws, {"method": "turn/completed", "params": {"threadId": THREAD, "turn": {"id": TURN}}})
        ws.recv(timeout=2)
    with fake_server(handler) as (endpoint, _):
        with AppServerConnection(endpoint) as client:
            assert client.drain(timeout=1) == [{"kind": "notification", "method": "turn/completed", "threadId": THREAD, "turnId": TURN}]
            assert client.drain() == []


def test_idle_connection_survives_request_timeout_interval():
    with fake_server(echo) as (endpoint, _):
        with AppServerConnection(endpoint, timeout=0.05) as client:
            time.sleep(0.12)
            assert client.request("thread/loaded/list", {}) == {"ok": True}


@pytest.mark.parametrize("mutating", [True, False])
@pytest.mark.parametrize("outcome", ["timeout", "disconnect", "mismatch", "malformed", "too_large", "event_flood"])
def test_uncertain_outcomes_close_connection_and_never_retry(mutating, outcome):
    def handler(ws, received):
        request = json.loads(ws.recv())
        received.append(request)
        if outcome == "timeout":
            time.sleep(0.15)
        elif outcome == "disconnect":
            ws.close()
        elif outcome == "mismatch":
            send(ws, {"id": "wrong", "result": {"secret": SECRET}})
        elif outcome == "malformed":
            ws.send(SECRET)
        elif outcome == "too_large":
            ws.send("x" * 1025)
        elif outcome == "event_flood":
            for _ in range(5):
                send(ws, {"method": "warning", "params": {"secret": SECRET}})
    with fake_server(handler) as (endpoint, received):
        with AppServerConnection(endpoint, timeout=0.08, max_message_bytes=1024, max_events=4) as client:
            with pytest.raises(AppServerError) as caught:
                client.request("turn/start", {}, mutating=mutating)
            assert caught.value.ambiguous is mutating
            assert SECRET not in str(caught.value)
            assert caught.value.__cause__ is None
            with pytest.raises(AppServerError, match="not_connected") as second:
                client.request("turn/start", {}, mutating=True)
            assert second.value.ambiguous is False
        assert len([request for request in received if request["method"] == "turn/start"]) == 1


@pytest.mark.parametrize("rpc_code", [-32601, -32602])
def test_method_or_parameter_rejection_is_definite_and_does_not_leak_server_content(rpc_code):
    def handler(ws, _):
        request = json.loads(ws.recv())
        send(ws, {"id": request["id"], "error": {"code": rpc_code, "message": SECRET, "data": SECRET}})
        request = json.loads(ws.recv())
        send(ws, {"id": request["id"], "result": {}})
    with fake_server(handler) as (endpoint, _):
        with AppServerConnection(endpoint) as client:
            with pytest.raises(AppServerError) as caught:
                client.request("turn/start", {}, mutating=True)
            assert caught.value.code == "appserver_rpc_error"
            assert caught.value.rpc_code == rpc_code
            assert caught.value.ambiguous is False
            assert SECRET not in str(caught.value)
            assert client.request("thread/loaded/list", {}) == {}


@pytest.mark.parametrize("rpc_code", [-32600, -32603, -32000, 123])
@pytest.mark.parametrize("mutating", [True, False])
def test_runtime_rpc_errors_are_ambiguous_for_mutations_and_connection_remains_usable(rpc_code, mutating):
    def handler(ws, _):
        request = json.loads(ws.recv())
        send(ws, {"id": request["id"], "error": {"code": rpc_code, "message": SECRET}})
        request = json.loads(ws.recv())
        send(ws, {"id": request["id"], "result": {}})
    with fake_server(handler) as (endpoint, _):
        with AppServerConnection(endpoint) as client:
            with pytest.raises(AppServerError) as caught:
                client.request("turn/start", {}, mutating=mutating)
            assert caught.value.ambiguous is mutating
            assert caught.value.code == "appserver_rpc_error"
            assert caught.value.rpc_code == rpc_code
            assert SECRET not in str(caught.value)
            assert client.request("thread/loaded/list", {}) == {}


def test_outgoing_limit_is_definite_and_never_sent():
    with fake_server(echo) as (endpoint, received):
        with AppServerConnection(endpoint, max_message_bytes=512) as client:
            with pytest.raises(AppServerError, match="request_too_large") as caught:
                client.request("turn/start", {"text": SECRET * 100}, mutating=True)
            assert caught.value.ambiguous is False
        assert len(received) == 2


def test_send_failure_after_attempt_is_ambiguous_without_retry():
    class Broken:
        def send(self, _):
            raise OSError(SECRET)
        def close(self):
            pass
    client = AppServerConnection("unix:///tmp/private/rpc.sock")
    client._socket = Broken()
    with pytest.raises(AppServerError) as caught:
        client.request("turn/start", {}, mutating=True)
    assert caught.value.ambiguous is True
    assert SECRET not in str(caught.value)


def test_send_timeout_is_bounded_even_when_peer_does_not_read():
    class Stalled:
        def __init__(self):
            self.stopped = threading.Event()
            self.socket = self
            self.sends = 0
        def send(self, _):
            self.sends += 1
            assert self.stopped.wait(timeout=1)
            raise OSError(SECRET)
        def shutdown(self, _):
            self.stopped.set()
        def close(self):
            pass
    stalled = Stalled()
    client = AppServerConnection("unix:///tmp/private/rpc.sock", timeout=0.05)
    client._socket = stalled
    started = time.monotonic()
    with pytest.raises(AppServerError) as caught:
        client.request("turn/start", {}, mutating=True)
    assert caught.value.code == "appserver_timeout"
    assert caught.value.ambiguous is True
    assert stalled.sends == 1
    assert time.monotonic() - started < 0.5


@pytest.mark.skipif(not hasattr(socket, "SO_PEERCRED"), reason="Linux peer credentials")
def test_foreign_peer_uid_rejected_before_websocket_handshake():
    class ForeignPeer:
        def getsockopt(self, level, option, length):
            assert (level, option, length) == (socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            return struct.pack("3i", os.getpid(), os.getuid() + 1, os.getgid())
    with pytest.raises(AppServerError, match="unsafe_appserver_peer"):
        _check_peer(ForeignPeer())


@pytest.mark.parametrize("endpoint,token_file", [
    ("ws://127.0.0.1:2345/rpc", None),
    ("ws://localhost:2345/rpc", "token"),
    ("ws://192.0.2.1:2345/rpc", "token"),
    ("wss://127.0.0.1:2345/rpc", "token"),
    ("ws://user:secret@127.0.0.1:2345/rpc", "token"),
    ("ws://127.0.0.1:2345/rpc?token=secret", "token"),
    ("ws://127.0.0.1:2345/rpc#fragment", "token"),
    ("ws://127.0.0.1/rpc", "token"),
    ("unix://relative/socket", None),
    ("unix:///tmp/../private/socket", None),
    ("unix:///tmp/private/socket", "token"),
    ("unix:///tmp/private/%73ocket", None),
])
def test_endpoint_restrictions(endpoint, token_file):
    with pytest.raises(AppServerError, match="invalid_appserver_endpoint"):
        validate_endpoint(endpoint, token_file=token_file)


def test_literal_ipv6_loopback_is_accepted_without_dns():
    assert validate_endpoint("ws://[::1]:2345/rpc", token_file="token")[0] == "ws"


def test_authenticated_loopback_uses_private_token_and_bypasses_proxy(tmp_path, monkeypatch, caplog):
    token_file = tmp_path / "private" / "token.json"
    write_private_json(token_file, {"token": SECRET})
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("WS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    def handler(ws, received):
        assert ws.request.headers["Authorization"] == "Bearer " + SECRET
        echo(ws, received)
    with fake_server(handler, tcp=True) as (endpoint, _):
        with AppServerConnection(endpoint, token_file=token_file) as client:
            assert client.request("thread/loaded/list", {}) == {"ok": True}
    assert SECRET not in caplog.text


@pytest.mark.parametrize("mode", ["public", "symlink", "invalid_token"])
def test_unsafe_token_file_rejected_before_tcp_connect(tmp_path, mode):
    token_file = tmp_path / "private" / "token.json"
    write_private_json(token_file, {"token": SECRET})
    if mode == "public":
        token_file.chmod(0o644)
    elif mode == "symlink":
        link = token_file.parent / "link.json"
        link.symlink_to(token_file)
        token_file = link
    else:
        write_private_json(token_file, {"token": "hello\r\nsecret"}, overwrite=True)
    with pytest.raises(AppServerError) as caught:
        AppServerConnection("ws://127.0.0.1:1/rpc", token_file=token_file).connect()
    assert caught.value.code in ("appserver_credentials_unavailable", "invalid_appserver_credentials")
    assert caught.value.ambiguous is False
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("kind", ["public_parent", "public_socket", "symlink_parent", "regular_file"])
def test_unsafe_unix_paths_rejected(kind):
    with tempfile.TemporaryDirectory(prefix="aot-ws-") as directory:
        parent = Path(directory).resolve() / "private"
        parent.mkdir(mode=0o700)
        path = parent / "rpc.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.bind(str(path))
            path.chmod(0o600)
            if kind == "public_parent":
                parent.chmod(0o755)
            elif kind == "public_socket":
                path.chmod(0o666)
            elif kind == "symlink_parent":
                link = Path(directory).resolve() / "alias"
                link.symlink_to(parent)
                path = link / "rpc.sock"
            else:
                path.unlink()
                path.write_text("not a socket")
                path.chmod(0o600)
            with pytest.raises(AppServerError, match="unsafe_appserver_socket"):
                AppServerConnection("unix://" + str(path)).connect()


def test_private_absolute_leaf_alias_connects_to_private_socket():
    with fake_server(echo) as (endpoint, _), tempfile.TemporaryDirectory(prefix="aot-alias-") as directory:
        alias = Path(directory).resolve() / "configured.sock"
        alias.symlink_to(endpoint.removeprefix("unix://"))
        with AppServerConnection("unix://" + str(alias)) as client:
            assert client.request("thread/loaded/list", {}) == {"ok": True}


@pytest.mark.parametrize("kind", ["relative", "chain", "public_alias_parent", "public_target_parent", "foreign_alias_owner", "target_parent_alias"])
def test_unsafe_socket_aliases_rejected(kind, monkeypatch):
    with tempfile.TemporaryDirectory(prefix="aot-alias-") as directory:
        root = Path(directory).resolve()
        alias_parent = root / "config"
        target_parent = root / "backend"
        alias_parent.mkdir(mode=0o700)
        target_parent.mkdir(mode=0o700)
        target = target_parent / "actual.sock"
        alias = alias_parent / "configured.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as raw:
            raw.bind(str(target))
            target.chmod(0o600)
            if kind == "relative":
                alias.symlink_to("../backend/actual.sock")
            elif kind == "chain":
                middle = target_parent / "middle.sock"
                middle.symlink_to(target)
                alias.symlink_to(middle)
            elif kind == "target_parent_alias":
                middle = root / "middle"
                middle.symlink_to(target_parent)
                alias.symlink_to(middle / "actual.sock")
            else:
                alias.symlink_to(target)
            if kind == "public_alias_parent":
                alias_parent.chmod(0o755)
            elif kind == "public_target_parent":
                target_parent.chmod(0o755)
            elif kind == "foreign_alias_owner":
                original_lstat = Path.lstat
                def lstat(path):
                    info = original_lstat(path)
                    if path == alias:
                        return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1,
                                               st_dev=info.st_dev, st_ino=info.st_ino)
                    return info
                monkeypatch.setattr(Path, "lstat", lstat)
            with pytest.raises(AppServerError, match="unsafe_appserver_socket"):
                AppServerConnection("unix://" + str(alias)).connect()


def test_endpoint_identity_change_during_connect_is_rejected(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="aot-alias-") as directory:
        path = Path(directory).resolve() / "actual.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
            path.chmod(0o600)
            listener.listen()
            original = appserver_client._private_socket
            calls = 0
            def changed_identity(path):
                nonlocal calls
                calls += 1
                result = original(path)
                return result if calls == 1 else (result[0], ("changed",))
            monkeypatch.setattr(appserver_client, "_private_socket", changed_identity)
            with pytest.raises(AppServerError, match="unsafe_appserver_socket"):
                AppServerConnection("unix://" + str(path)).connect()
            assert calls == 2
