"""Independent, offline security boundaries for captured-history pagination."""
import base64
import json
import sqlite3

import pytest

from agents_over_telegram.core import Core, CoreError
from agents_over_telegram.transport import FakeTransport


@pytest.fixture
def env(tmp_path):
    now = [2_000_000.0]
    transport = FakeTransport()
    core = Core(tmp_path / "private-history", transport, clock=lambda: now[0])
    core.register_account(transport.bot_id, transport.username)

    def incoming(chat, sender, text, **fields):
        number = len(transport.updates) + 1
        message = {"message_id": number, "chat": {"id": chat, "type": "supergroup", "title": "Offline"},
                   "from": {"id": sender, "is_bot": False}, "text": text, **fields}
        transport.updates.append({"update_id": number, "message": message})
        return number

    def bind(label, chat, owner):
        client = core.create_client(label)
        pair = core.connection_begin(client["client_id"])
        incoming(chat, owner, pair["command"])
        core.collect()
        core.confirm_pairing(pair["pairing_id"])
        return client["client_id"]

    alpha = bind("Alpha", -1001, 101)
    beta = bind("Beta", -1002, 102)
    return core, transport, now, incoming, bind, alpha, beta


def identity(message):
    return (message["direction"], message.get("delivery_id", message.get("outbox_id")))


def all_pages(core, client, first):
    page = first
    result = []
    while True:
        result.extend(page["messages"])
        if page["next_cursor"] is None:
            return result
        page = core.history(client, limit=1, cursor=page["next_cursor"])


def snapshot_database(core):
    with sqlite3.connect(core.database) as db:
        return {table: db.execute("SELECT * FROM " + table + " ORDER BY rowid").fetchall()
                for table in ("account", "inbox", "outbox", "conversations", "rate_limits", "history_keys")}


def test_same_timestamp_snapshot_excludes_later_inserts_in_both_streams(env):
    core, transport, _, incoming, _, alpha, beta = env
    incoming(-1001, 101, "Alpha old one")
    incoming(-1002, 102, "Beta private one")
    incoming(-1001, 101, "Alpha old two")
    core.collect()
    core.send(alpha, "Alpha old outgoing one", "history-old-1")
    core.send(beta, "Beta private outgoing", "history-beta-1")
    core.send(alpha, "Alpha old outgoing two", "history-old-2")
    baseline = core.history(alpha, limit=50)
    expected = [identity(message) for message in baseline["messages"]]
    first = core.history(alpha, limit=1)
    assert first["next_cursor"]

    # Same event timestamp means a timestamp-only cursor would admit these rows.
    incoming(-1001, 101, "Alpha new incoming")
    incoming(-1002, 102, "Beta private two")
    core.collect()
    core.send(alpha, "Alpha new outgoing", "history-new-1")
    paged = all_pages(core, alpha, first)
    assert [identity(message) for message in paged] == expected
    assert len({identity(message) for message in paged}) == len(expected)
    assert "Beta private" not in json.dumps(paged)
    current = core.history(alpha, limit=50)["messages"]
    assert len(current) == len(expected) + 2


def test_cursor_cannot_cross_clients_or_be_retargeted(env):
    core, _, _, incoming, _, alpha, beta = env
    for text in ("Alpha first", "Alpha second"):
        incoming(-1001, 101, text)
    incoming(-1002, 102, "Beta private")
    core.collect()
    cursor = core.history(alpha, limit=1)["next_cursor"]
    with pytest.raises(CoreError, match="history_cursor_invalid"):
        core.history(beta, cursor=cursor)
    encoded, signature = cursor.split(".")
    original = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    modifications = {
        "client": beta,
        "at": original["at"] + 1,
        "inbox_max": original["inbox_max"] + 100,
        "outbox_max": original["outbox_max"] + 100,
        "last": [original["last"][0], original["last"][1], original["last"][2] + 1],
    }
    for field, replacement in modifications.items():
        modified = dict(original, **{field: replacement})
        body = base64.urlsafe_b64encode(json.dumps(modified).encode()).rstrip(b"=").decode()
        with pytest.raises(CoreError, match="history_cursor_invalid"):
            core.history(alpha, cursor=body + "." + signature)


def test_revoked_cursor_and_rebound_destination_cannot_access_old_client_history(env):
    core, _, _, incoming, bind, alpha, beta = env
    incoming(-1001, 101, "Alpha confidential one")
    incoming(-1001, 101, "Alpha confidential two")
    core.collect()
    cursor = core.history(alpha, limit=1)["next_cursor"]
    core.disconnect(alpha)
    with pytest.raises(CoreError, match="client_revoked"):
        core.history(alpha, cursor=cursor)
    replacement = bind("Replacement", -1001, 101)
    assert core.history(replacement)["messages"] == []
    with pytest.raises(CoreError, match="history_cursor_invalid"):
        core.history(replacement, cursor=cursor)
    assert core.history(beta)["messages"] == []


def test_acked_history_is_context_only_without_leases_or_transport_side_effects(env):
    core, transport, _, incoming, _, alpha, _ = env
    incoming(-1001, 101, "Handled already")
    incoming(-1001, 101, "Still pending")
    core.collect()
    claimed = core.poll(alpha, limit=1)
    core.ack(alpha, [claimed["deliveries"][0]["delivery_id"]], claimed["lease_token"])
    core.send(alpha, "Old outgoing", "history-readonly-outgoing")
    before = snapshot_database(core)
    sent = list(transport.sent)
    offsets = list(transport.poll_offsets)
    result = all_pages(core, alpha, core.history(alpha, limit=1))
    assert {message["status"] for message in result if message["direction"] == "incoming"} == {"acknowledged", "pending"}
    assert all(message["context_only"] for message in result)
    assert not any("lease_token" in message or "lease_until" in message for message in result)
    assert snapshot_database(core) == before
    assert transport.sent == sent
    assert transport.poll_offsets == offsets


def test_cursor_window_is_frozen_until_exact_expiry(env):
    core, _, now, incoming, _, alpha, _ = env
    incoming(-1001, 101, "At lower boundary", date=int(now[0] - core.HISTORY_WINDOW))
    incoming(-1001, 101, "Current event", date=int(now[0]))
    core.collect()
    first = core.history(alpha, limit=1)
    cursor = first["next_cursor"]
    assert cursor
    now[0] += core.HISTORY_CURSOR_TTL - 1
    page = core.history(alpha, cursor=cursor)
    assert [message["text"] for message in page["messages"]] == ["At lower boundary"]
    assert page["coverage"]["window_start"] == first["coverage"]["window_start"]
    assert page["coverage"]["window_end"] == first["coverage"]["window_end"]
    now[0] += 1
    with pytest.raises(CoreError, match="history_cursor_expired"):
        core.history(alpha, cursor=cursor)


def test_unbound_captured_updates_are_not_imported_into_later_history(env):
    core, _, _, incoming, bind, _, _ = env
    incoming(-1003, 103, "Before binding private message")
    core.collect()
    later = bind("Later", -1003, 103)
    assert core.history(later)["messages"] == []


def test_broker_history_exposes_only_authorized_publication_context(env):
    core, _, _, _, _, alpha, beta = env
    core.grant_peer(alpha, beta)
    outbox = core.send(alpha, "Shared context", "history-peer", recipient=beta)
    core.send(alpha, "Alpha only private outgoing", "history-alpha-private")
    beta_history = core.history(beta)["messages"]
    assert len(beta_history) == 1
    assert beta_history[0]["origin"] == "broker"
    assert beta_history[0]["logical_sender"]["client_id"] == alpha
    assert beta_history[0]["publication"]["outbox_id"] == outbox["outbox_id"]
    assert "Alpha only private outgoing" not in json.dumps(beta_history)
