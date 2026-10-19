"""Repeatable acceptance example with no Telegram credentials or network."""
from __future__ import annotations
import tempfile
from .core import Core, CoreError
from .transport import FakeTransport


def _update(update_id, text, thread_id, *, sender_id=101, chat_id=-100123):
    return {"update_id": update_id, "message": {"message_id": update_id,
        "chat": {"id": chat_id, "type": "supergroup", "title": "Isolated offline example"},
        "message_thread_id": thread_id,
        "from": {"id": sender_id, "is_bot": False}, "text": text}}


def run_demo():
    now = [1000.0]
    transport = FakeTransport()
    with tempfile.TemporaryDirectory(prefix="aot-offline-demo-") as state:
        core = Core(state, transport, clock=lambda: now[0])
        bot = transport.get_me()
        core.register_account(bot["id"], bot["username"])
        alpha, beta = core.create_client("Alpha"), core.create_client("Beta")
        a, b = alpha["client_id"], beta["client_id"]
        pair_a, pair_b = core.connection_begin(a), core.connection_begin(b)
        transport.updates.extend([_update(1, pair_a["command"], 11), _update(2, pair_b["command"], 22)])
        core.collect()
        core.confirm_pairing(pair_a["pairing_id"])
        core.confirm_pairing(pair_b["pairing_id"])
        transport.updates.extend([_update(3, "Alpha request", 11), _update(4, "Beta request", 22),
                                  _update(5, "Unknown sender never reaches either agent", 11, sender_id=999)])
        intake = core.collect()
        page_a, page_b = core.poll(a), core.poll(b)
        assert [m["text"] for m in page_a["deliveries"]] == ["Alpha request"]
        assert [m["text"] for m in page_b["deliveries"]] == ["Beta request"]
        cross_ack_blocked = False
        try:
            core.ack(a, [page_b["deliveries"][0]["delivery_id"]], page_b["lease_token"])
        except CoreError:
            cross_ack_blocked = True
        assert cross_ack_blocked
        for client, page in ((a,page_a), (b,page_b)):
            core.ack(client, [m["delivery_id"] for m in page["deliveries"]], page["lease_token"])
        core.grant_peer(a, b)
        published = core.send(a, "Broker handoff", "handoff-1", recipient=b)
        handoff = core.poll(b)
        assert len(handoff["deliveries"]) == 1
        assert handoff["deliveries"][0]["origin"] == "broker"
        assert len(transport.updates) == 5  # Telegram never echoed outbound messages.
        repeated = core.send(a, "Broker handoff", "handoff-1", recipient=b)
        assert repeated["outbox_id"] == published["outbox_id"]
        assert len(transport.sent) == 1
        now[0] += 2
        transport.outcomes.append("unknown")
        uncertain = core.send(a, "Accepted remotely but response lost", "unknown-1")
        assert uncertain["status"] == "unknown"
        restarted = Core(state, transport, clock=lambda: now[0])
        restarted.flush(a)
        assert restarted.delivery_status(a, uncertain["outbox_id"])["status"] == "unknown"
        assert len(transport.sent) == 2
        restarted.pause(a)
        transport.updates.extend([_update(6, "Retained during pause", 11), _update(7, "Beta still active", 22)])
        restarted.collect()
        assert restarted.poll(a)["deliveries"] == []
        assert any(m["text"] == "Beta still active" for m in restarted.poll(b)["deliveries"])
        restarted.pause(a, False)
        assert any(m["text"] == "Retained during pause" for m in restarted.poll(a)["deliveries"])
        history_page = restarted.history(a, limit=1)
        history = list(history_page["messages"])
        while history_page["next_cursor"]:
            history_page = restarted.history(a, limit=1, cursor=history_page["next_cursor"])
            history.extend(history_page["messages"])
        assert any(m["text"] == "Alpha request" and m["status"] == "acknowledged" for m in history)
        assert any(m["direction"] == "outgoing" and m["status"] == "unknown" for m in history)
        assert not any(m["text"] == "Beta request" for m in history)
        assert all(m["context_only"] for m in history)
        assert len(transport.sent) == 2
        now[0] += 86401
        assert restarted.history(a)["messages"] == []
        return {"result": "PASS", "network": "none", "credentials": "none", "bot_accounts": 1,
            "agents": 2, "dedicated_topics": [11,22], "shared_cursor_after_first_intake": intake["cursor"],
            "cross_agent_ack_blocked": cross_ack_blocked, "same_bot_delivery_origin": "broker",
            "duplicate_send_suppressed": True, "unknown_send_survived_restart_without_retry": True,
            "pause_retained_work_and_other_agent_continued": True, "temporary_state_removed": True,
            "history_24h_read_only_context": True, "acknowledged_history_available": True,
            "history_pagination_isolated": True, "history_cutoff_verified": True}
