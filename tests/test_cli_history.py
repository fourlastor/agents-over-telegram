"""The operator history command reads without credentials or delivery mutations."""
import json
import sqlite3
import subprocess
import sys
import time

from agents_over_telegram.core import Core
from agents_over_telegram.credentials import write_private_json
from agents_over_telegram.transport import FakeTransport


def test_cli_history_reads_acknowledged_context_without_transport_or_recovery(tmp_path):
    state = tmp_path / "state"
    fake = FakeTransport()
    core = Core(state, fake)
    core.register_account(fake.bot_id, fake.username)
    credential = core.create_client("History agent")
    client_id = credential["client_id"]
    client_file = state / "clients" / "history.json"
    write_private_json(client_file, credential)
    pair = core.connection_begin(client_id)
    def message(update_id, text):
        return {"update_id": update_id, "message": {"message_id": update_id, "date": int(time.time()),
            "chat": {"id": -100, "type": "supergroup"}, "from": {"id": 101, "is_bot": False}, "text": text}}
    fake.updates.append(message(1, pair["command"]))
    core.collect()
    core.confirm_pairing(pair["pairing_id"])
    fake.updates.extend([message(2, "Handled context"), message(3, "Pending work")])
    core.collect()
    leased = core.poll(client_id)
    handled = next(item for item in leased["deliveries"] if item["text"] == "Handled context")
    core.ack(client_id, [handled["delivery_id"]], leased["lease_token"])
    sent = core.send(client_id, "Sending state must remain untouched", "intent")
    with sqlite3.connect(core.database) as db:
        db.execute("UPDATE outbox SET status='sending',message_id=NULL WHERE id=?", (sent["outbox_id"],))
    def delivery_state():
        with sqlite3.connect(core.database) as db:
            return {table: db.execute("SELECT * FROM " + table).fetchall() for table in ("inbox", "outbox", "account", "rate_limits")}
    before = delivery_state()
    # No credentials.json exists and no --fake option is used: a network/token
    # attempt would fail; this command must work entirely from private local data.
    process = subprocess.run([sys.executable, "-m", "agents_over_telegram", "--state-dir", str(state),
        "history", "--client-file", str(client_file), "--limit", "1"], capture_output=True, text=True, timeout=10)
    assert process.returncode == 0, process.stderr
    page = json.loads(process.stdout)
    assert page["context_only"] and page["next_cursor"]
    texts = [row["text"] for row in page["messages"]]
    while page["next_cursor"]:
        process = subprocess.run([sys.executable, "-m", "agents_over_telegram", "--state-dir", str(state),
            "history", "--client-file", str(client_file), "--limit", "1", "--cursor", page["next_cursor"]],
            capture_output=True, text=True, timeout=10)
        assert process.returncode == 0, process.stderr
        page = json.loads(process.stdout)
        texts.extend(row["text"] for row in page["messages"])
    assert "Handled context" in texts and "Pending work" in texts
    assert any("Sending state must remain untouched" in text for text in texts)
    assert delivery_state() == before
    assert not (state / "credentials.json").exists()
    assert not (state / "fake-transport.json").exists()
