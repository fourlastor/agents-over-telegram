"""Process-level relay waiting and control, with offline Telegram transport."""
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time

import pytest

from agents_over_telegram.cli import parser, run
from agents_over_telegram.core import Core, CoreError
from agents_over_telegram.credentials import CredentialError, read_private_json, write_private_json
from agents_over_telegram.relay import RelayStore
from agents_over_telegram.transport import FileFakeTransport
from test_setup import prepare, connect


THREAD = '01a153ec-3a02-7cb1-9168-b23584e47381'


@pytest.fixture
def relay(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    profile = prepare(tmp_path)
    connect(profile)
    state = Path(profile['state_dir'])
    core = Core(state, FileFakeTransport(state / 'fake-transport.json'), recover_sends=False)
    store = RelayStore(core)
    store.bind(profile['client_id'], THREAD, profile['project_dir'])
    return profile, core, store


def add_update(profile):
    path = Path(profile['state_dir']) / 'fake-transport.json'
    value = read_private_json(path)
    number = max(x['update_id'] for x in value['updates']) + 1
    value['updates'].append({'update_id': number, 'message': {
        'message_id': number, 'chat': {'id': -100, 'type': 'supergroup'},
        'from': {'id': 123, 'is_bot': False}, 'text': 'untrusted text must not appear in wake'}})
    write_private_json(path, value, overwrite=True)


def launch(profile, *options, stdin=subprocess.PIPE):
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
    return subprocess.Popen([sys.executable, '-m', 'agents_over_telegram', '--state-dir',
                             profile['state_dir'], '--fake', 'relay-watch', *options],
                            stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1, env=env)


def line(process, timeout=5):
    # TextIO read-ahead is avoided by reading only one line per emitted event.
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(timeout), 'watcher did not emit within test deadline'
    return json.loads(process.stdout.readline())


def finish(process):
    if process.poll() is None:
        process.terminate()
    out, err = process.communicate(timeout=5)
    assert process.returncode == 0, err
    return [json.loads(x) for x in out.splitlines() if x.strip()]


def test_once_collect_emits_without_leasing_acknowledging_or_replying(relay):
    profile, core, store = relay
    add_update(profile)
    process = launch(profile, '--once', '--collect', '--duration', '4', '--interval', '.1')
    out, err = process.communicate(timeout=6)
    assert process.returncode == 0, err
    values = [json.loads(x) for x in out.splitlines()]
    events = [x for x in values if x['type'] == 'telegram_wake']
    assert len(events) == 1 and events[0]['thread_id'] == THREAD
    assert 'untrusted text' not in out
    with core._db() as db:
        row = db.execute('SELECT acknowledged,lease_token,lease_until FROM inbox').fetchone()
        assert tuple(row) == (0, None, None)
        assert db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 0
    assert read_private_json(Path(profile['state_dir']) / 'fake-transport.json')['sent'] == []
    store.resolve(events[0]['notification_id'], 'accepted')
    assert store.next_event() is None


def test_no_collect_is_quiet_and_does_not_poll_or_load_token(relay):
    profile, core, store = relay
    path = Path(profile['state_dir']) / 'fake-transport.json'
    before = path.read_bytes()
    process = launch(profile, '--once', '--duration', '.3')
    out, err = process.communicate(timeout=3)
    assert process.returncode == 0, err
    values = [json.loads(x) for x in out.splitlines()]
    assert [x['type'] for x in values] == ['relay_ready', 'relay_stopped']
    assert values[-1]['reason'] == 'timeout'
    assert path.read_bytes() == before
    assert not (Path(profile['state_dir']) / 'credentials.json').exists()


def test_persistent_receipt_and_stop_leave_processing_to_target(relay):
    profile, core, store = relay
    process = launch(profile, '--duration', '5', '--interval', '.1')
    try:
        assert line(process)['type'] == 'relay_ready'
        add_update(profile)
        core.collect()
        event = line(process)
        assert event['type'] == 'telegram_wake'
        process.stdin.write(json.dumps({'command': 'resolve', 'notification_id': event['notification_id'],
                                        'outcome': 'accepted'}) + '\n')
        process.stdin.flush()
        assert line(process)['type'] == 'relay_receipt'
        process.stdin.write('{"command":"stop"}\n')
        process.stdin.flush()
        out, err = process.communicate(timeout=3)
        assert process.returncode == 0, err
        assert json.loads(out.splitlines()[-1])['reason'] == 'requested'
        assert len(core.poll(profile['client_id'])['deliveries']) == 1
    finally:
        if process.poll() is None:
            finish(process)


def test_closed_stdin_exits_without_collection_and_releases_lock(relay):
    profile, core, store = relay
    path = Path(profile['state_dir']) / 'fake-transport.json'
    before = path.read_bytes()
    process = launch(profile, '--collect', '--duration', '5', stdin=subprocess.DEVNULL)
    out, err = process.communicate(timeout=3)
    assert process.returncode == 0, err
    assert json.loads(out.splitlines()[-1])['reason'] == 'stdin_closed'
    assert path.read_bytes() == before
    with core._lock('relay-watcher') as acquired:
        assert acquired


def test_concurrent_watcher_rejected_and_shutdown_holds_unresolved(relay):
    profile, core, store = relay
    process = launch(profile, '--duration', '5', '--interval', '.1')
    try:
        assert line(process)['type'] == 'relay_ready'
        second = launch(profile, '--once', '--duration', '.2')
        out, err = second.communicate(timeout=3)
        assert second.returncode == 1 and 'relay_watcher_busy' in err
        add_update(profile)
        core.collect()
        assert line(process)['type'] == 'telegram_wake'
        process.stdin.close()
        process.stdin = None
        process.communicate(timeout=3)
        assert process.returncode == 0
        assert store.next_event() is None  # unresolved handoff is held on shutdown
        with core._lock('relay-watcher') as acquired:
            assert acquired
    finally:
        if process.poll() is None:
            finish(process)


def test_cli_binding_checks_profile_identity_before_routing(relay, tmp_path):
    profile, core, store = relay
    args = ['--state-dir', profile['state_dir'], '--fake', 'relay-bind', '--client-file',
            profile['client_file'], '--thread-id', THREAD, '--project-dir', str(tmp_path)]
    with pytest.raises(CredentialError, match='relay_project_profile_mismatch'):
        run(parser().parse_args(args))
    args[-1] = profile['project_dir']
    assert run(parser().parse_args(args))


def test_revoked_client_can_inspect_and_remove_relay_but_not_rebind(relay):
    profile, core, store = relay
    core.disconnect(profile['client_id'])
    common = ['--state-dir', profile['state_dir'], '--fake']
    scoped = ['--client-file', profile['client_file']]
    assert run(parser().parse_args(common + ['relay-status'] + scoped))['bindings']
    assert run(parser().parse_args(common + ['relay-unbind'] + scoped))['enabled'] is False
    with pytest.raises(CoreError, match='authentication_failed'):
        run(parser().parse_args(common + ['relay-bind'] + scoped + ['--thread-id', THREAD,
                            '--project-dir', profile['project_dir']]))


def test_profile_drift_blocks_wake_without_losing_work(relay):
    profile, core, store = relay
    config = Path(profile['config_file'])
    saved = config.read_text()
    config.write_text(saved.replace('agents-over-telegram', 'wrong-runtime'))
    add_update(profile)
    core.collect()
    process = launch(profile, '--once', '--duration', '.2')
    out, err = process.communicate(timeout=3)
    assert process.returncode == 0, err
    assert 'telegram_wake' not in out
    config.write_text(saved)
    process = launch(profile, '--once', '--duration', '.5')
    out, err = process.communicate(timeout=3)
    assert process.returncode == 0, err
    assert 'telegram_wake' in out


@pytest.mark.parametrize('timeout', [-1, 21, True, 1.5])
def test_collection_timeout_rejected_before_transport(relay, timeout):
    profile, core, store = relay
    path = Path(profile['state_dir']) / 'fake-transport.json'
    before = path.read_bytes()
    with pytest.raises(CoreError, match='invalid_collect_timeout'):
        core.collect(timeout=timeout)
    assert path.read_bytes() == before
