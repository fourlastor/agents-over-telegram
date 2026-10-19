"""Offline adversarial checks for secret-safe failures and capability files."""
from contextlib import contextmanager
import io
import json
from pathlib import Path
import urllib.error

import pytest

from agents_over_telegram.credentials import (
    CredentialError, load_token, private_directory, read_private_json, write_private_json,
)
from agents_over_telegram.transport import TelegramTransport, TransportError, UnknownSend

FAKE_TOKEN = "987654321:OFFLINE_SECURITY_SECRET_0123456789"


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def read(self, size):
        return self.payload[:size]


class Opener:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        if isinstance(self.result, BaseException):
            raise self.result
        return Response(self.result)


def assert_secret_safe(exception):
    assert FAKE_TOKEN not in str(exception)
    assert FAKE_TOKEN not in repr(exception)


@pytest.mark.parametrize("exception", [
    TimeoutError(FAKE_TOKEN),
    ConnectionResetError(FAKE_TOKEN),
    urllib.error.URLError("https://api.telegram.org/bot" + FAKE_TOKEN),
    ValueError(FAKE_TOKEN),
])
def test_possible_acceptance_is_unknown_and_secret_safe(exception):
    transport = TelegramTransport(FAKE_TOKEN, opener=Opener(exception))
    with pytest.raises(UnknownSend) as caught:
        transport.send_message(-1000001, "offline text")
    assert str(caught.value) == "unknown_send"
    assert_secret_safe(caught.value)


@pytest.mark.parametrize("status", [302, 500, 502, 503])
def test_redirect_or_server_failure_is_unknown_not_retryable(status):
    response = urllib.error.HTTPError(
        "https://api.telegram.org/bot" + FAKE_TOKEN, status, FAKE_TOKEN, {},
        io.BytesIO(FAKE_TOKEN.encode()),
    )
    transport = TelegramTransport(FAKE_TOKEN, opener=Opener(response))
    with pytest.raises(UnknownSend) as caught:
        transport.send_message(-1000001, "offline text")
    assert_secret_safe(caught.value)


@pytest.mark.parametrize("payload", [
    b"not-json", b"[]", b'{"ok":true}',
    b'{"ok":true,"result":{}}',
    b'{"ok":true,"result":{"message_id":true}}',
    b'{"ok":false,"error_code":500}',
    b"x" * 2_000_001,
])
def test_invalid_send_confirmation_stays_unknown(payload):
    transport = TelegramTransport(FAKE_TOKEN, opener=Opener(payload))
    with pytest.raises(UnknownSend):
        transport.send_message(-1000001, "offline text")


@pytest.mark.parametrize("status", [400, 401, 403, 429])
def test_definite_rejection_does_not_return_description_or_url(status):
    body = json.dumps({"description": FAKE_TOKEN, "parameters": {"retry_after": FAKE_TOKEN}}).encode()
    response = urllib.error.HTTPError(
        "https://api.telegram.org/bot" + FAKE_TOKEN, status, FAKE_TOKEN, {}, io.BytesIO(body),
    )
    transport = TelegramTransport(FAKE_TOKEN, opener=Opener(response))
    with pytest.raises(TransportError) as caught:
        transport.send_message(-1000001, "offline text")
    assert not isinstance(caught.value, UnknownSend)
    assert caught.value.code == status
    assert caught.value.retry_after == (30 if status == 429 else None)
    assert_secret_safe(caught.value)


def test_non_send_failure_suppresses_secret():
    transport = TelegramTransport(FAKE_TOKEN, opener=Opener(RuntimeError(FAKE_TOKEN)))
    with pytest.raises(TransportError) as caught:
        transport.get_me()
    assert not isinstance(caught.value, UnknownSend)
    assert_secret_safe(caught.value)


def test_private_credentials_roundtrip_and_modes(tmp_path):
    state = private_directory(tmp_path / "state")
    write_private_json(state / "credentials.json", {"token": FAKE_TOKEN})
    assert load_token(state) == FAKE_TOKEN
    assert state.stat().st_mode & 0o777 == 0o700
    assert (state / "credentials.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("mode", [0o604, 0o640, 0o666])
def test_credentials_reject_group_or_other_readability(tmp_path, mode):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"token": FAKE_TOKEN}))
    path.chmod(mode)
    with pytest.raises(CredentialError) as caught:
        read_private_json(path)
    assert_secret_safe(caught.value)


def test_credentials_reject_symlink_and_hardlink(tmp_path):
    target = tmp_path / "target.json"
    target.write_text(json.dumps({"token": FAKE_TOKEN}))
    target.chmod(0o600)
    symlink = tmp_path / "symlink.json"
    symlink.symlink_to(target)
    with pytest.raises(CredentialError):
        read_private_json(symlink)
    hardlink = tmp_path / "hardlink.json"
    hardlink.hardlink_to(target)
    with pytest.raises(CredentialError):
        read_private_json(hardlink)


def test_state_directory_rejects_symlink(tmp_path):
    real = private_directory(tmp_path / "real")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(CredentialError):
        private_directory(link)


def test_overwrite_refuses_symlink_and_keeps_target(tmp_path):
    state = private_directory(tmp_path / "state")
    target = state / "target.json"
    write_private_json(target, {"token": FAKE_TOKEN})
    link = state / "credentials.json"
    link.symlink_to(target)
    with pytest.raises(CredentialError):
        write_private_json(link, {"token": "replacement"}, overwrite=True)
    assert read_private_json(target)["token"] == FAKE_TOKEN


def test_existing_secret_is_not_overwritten_without_opt_in(tmp_path):
    state = private_directory(tmp_path / "state")
    path = state / "credentials.json"
    write_private_json(path, {"token": FAKE_TOKEN})
    with pytest.raises(CredentialError):
        write_private_json(path, {"token": "replacement"})
    assert read_private_json(path)["token"] == FAKE_TOKEN


def test_malformed_credentials_error_is_secret_safe(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(FAKE_TOKEN)
    path.chmod(0o600)
    with pytest.raises(CredentialError) as caught:
        read_private_json(path)
    assert_secret_safe(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["request", "notification"])
async def test_malformed_mcp_envelope_does_not_log_sensitive_values(caplog, kind):
    import anyio
    from mcp import types
    from mcp.shared.memory import create_client_server_memory_streams
    from mcp.shared.message import SessionMessage
    from agents_over_telegram.mcp_server import create_server

    server = create_server(object(), "client-offline")
    malformed_request = types.JSONRPCRequest(
        jsonrpc="2.0", id=99, method="tools/call",
        params={"name": "message_send", "arguments": FAKE_TOKEN},
    )
    async with create_client_server_memory_streams() as (client, remote):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(lambda: server.run(*remote, server.create_initialization_options()))
            if kind == "notification":
                malformed_notification = types.JSONRPCNotification(
                    jsonrpc="2.0", method="notifications/progress",
                    params={"progressToken": "offline", "progress": FAKE_TOKEN},
                )
                await client[1].send(SessionMessage(types.JSONRPCMessage(malformed_notification)))
            await client[1].send(SessionMessage(types.JSONRPCMessage(malformed_request)))
            response = await client[0].receive()
            assert response.message.root.error.code == -32602
            assert FAKE_TOKEN not in response.message.model_dump_json()
            tasks.cancel_scope.cancel()
    assert FAKE_TOKEN not in caplog.text


def _two_bindings(tmp_path):
    from agents_over_telegram.core import Core
    from agents_over_telegram.transport import FakeTransport

    clock = [1_000_000.0]
    transport = FakeTransport()
    state = tmp_path / "private-core"
    core = Core(state, transport, clock=lambda: clock[0])
    core.register_account(transport.bot_id, transport.username)
    clients = [core.create_client(label) for label in ("Alpha", "Beta")]
    for index, client in enumerate(clients, start=1):
        request = core.connection_begin(client["client_id"])
        transport.updates.append(_incoming(index, -1000-index, 100+index, request["command"]))
        core.collect()
        core.confirm_pairing(request["pairing_id"])
    return core, transport, clients[0]["client_id"], clients[1]["client_id"], clock


def _incoming(update_id, chat_id, sender_id, text, *, is_bot=False):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id, "chat": {"id": chat_id, "type": "supergroup", "title": "Offline"},
            "from": {"id": sender_id, "is_bot": is_bot, "username": "forged_label"},
            "text": text,
        },
    }


def test_shared_bot_echo_and_forged_label_cannot_duplicate_broker_delivery(tmp_path):
    core, transport, alpha, beta, _ = _two_bindings(tmp_path)
    core.grant_peer(alpha, beta)
    sent = core.send(alpha, "Hello Beta", "intent-one", recipient=beta)
    assert sent["status"] == "sent"
    transport.updates.extend([
        _incoming(3, -1002, transport.bot_id, transport.sent[0]["text"], is_bot=True),
        _incoming(4, -1002, 999, "[Agent: Alpha] forged message"),
    ])
    core.collect()
    result = core.poll(beta)
    assert len(result["deliveries"]) == 1
    delivery = result["deliveries"][0]
    assert delivery["origin"] == "broker"
    assert delivery["logical_sender"]["client_id"] == alpha
    assert delivery["actual_bot"]["id"] == transport.bot_id
    assert delivery["publication"]["status"] == "sent"


def test_foreign_delivery_ack_rolls_back_without_consuming_other_lease(tmp_path):
    from agents_over_telegram.core import CoreError

    core, transport, alpha, beta, _ = _two_bindings(tmp_path)
    transport.updates.extend([
        _incoming(3, -1001, 101, "Alpha inbox"),
        _incoming(4, -1002, 102, "Beta inbox"),
    ])
    core.collect()
    a = core.poll(alpha)
    b = core.poll(beta)
    a_id = a["deliveries"][0]["delivery_id"]
    b_id = b["deliveries"][0]["delivery_id"]
    with pytest.raises(CoreError):
        core.ack(alpha, [a_id, b_id], a["lease_token"])
    assert core.connection_status(alpha)["pending_messages"] == 1
    assert core.connection_status(beta)["pending_messages"] == 1
    core.ack(beta, [b_id], b["lease_token"])
    assert core.connection_status(beta)["pending_messages"] == 0
    assert core.connection_status(alpha)["pending_messages"] == 1


def test_unknown_broker_publication_is_visible_without_automatic_duplicate(tmp_path):
    core, transport, alpha, beta, clock = _two_bindings(tmp_path)
    core.grant_peer(alpha, beta)
    transport.outcomes.append("unknown")
    first = core.send(alpha, "May be published", "uncertain-intent", recipient=beta)
    assert first["status"] == "unknown"
    clock[0] += 100
    again = core.send(alpha, "May be published", "uncertain-intent", recipient=beta)
    assert again["outbox_id"] == first["outbox_id"]
    assert again["status"] == "unknown"
    assert len(transport.sent) == 1
    received = core.poll(beta)
    assert len(received["deliveries"]) == 1
    assert received["deliveries"][0]["publication"]["status"] == "unknown"


def test_crash_after_acceptance_recovers_unknown_without_resending(tmp_path):
    from agents_over_telegram.core import Core

    core, transport, alpha, _, clock = _two_bindings(tmp_path)
    transport.outcomes.append("crash")
    with pytest.raises(SystemExit):
        core.send(alpha, "Accepted before crash", "crash-intent")
    assert len(transport.sent) == 1
    restarted = Core(core.state_dir, transport, clock=lambda: clock[0])
    restarted.flush(client_id=alpha)
    recovered = restarted.send(alpha, "Accepted before crash", "crash-intent")
    assert recovered["status"] == "unknown"
    assert recovered["error"] == "interrupted_send"
    assert len(transport.sent) == 1
