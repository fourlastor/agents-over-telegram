"""Older runtimes stand aside while a newer one collects for the same bot."""
import sqlite3

from agents_over_telegram.core import Core
from agents_over_telegram.onboarding import human_status
from test_core import append, bind, env  # noqa: F401 (env is a fixture)


def runtime(core, transport, clock, version):
    """Another runtime on the same state directory."""
    other = Core(core.state_dir, transport, clock=clock)
    other.runtime_version = version
    return other


def test_a_collection_records_the_runtime_version(env):
    core, transport, clock = env
    core.collect(timeout=1)
    status = core.collector_status()
    assert status['collector_version'] == core.runtime_version and status['collector_seen_at'] == clock.now
    assert status['deferring_to'] is None


def test_an_older_runtime_does_not_fetch_while_a_newer_one_collects(env):
    core, transport, clock = env
    client, _ = bind(core, transport)
    newer = runtime(core, transport, clock, '99.0.0')
    newer.collect(timeout=1)
    append(transport, 'routed by the newer runtime')
    clock.advance(30)
    result = core.collect()
    assert result == {'status': 'busy', 'updates': 0, 'reason': 'newer_runtime', 'collector_version': '99.0.0'}
    assert core.poll(client)['deliveries'] == []
    assert newer.collect()['updates'] == 1
    assert [d['text'] for d in core.poll(client)['deliveries']] == ['routed by the newer runtime']


def test_an_older_runtime_collects_again_once_the_newer_one_is_gone(env):
    core, transport, clock = env
    client, _ = bind(core, transport)
    runtime(core, transport, clock, '99.0.0').collect(timeout=1)
    append(transport, 'hello')
    clock.advance(core.COLLECTOR_STALE)
    assert core.collect(timeout=1)['status'] == 'ok'
    assert core.collector_status()['collector_version'] == core.runtime_version
    assert [d['text'] for d in core.poll(client)['deliveries']] == ['hello']


def test_a_newer_runtime_ignores_an_older_record(env):
    core, transport, clock = env
    runtime(core, transport, clock, '0.1.0').collect(timeout=1)
    assert core.collect(timeout=1)['status'] == 'ok'
    assert core.collector_status()['collector_version'] == core.runtime_version


def test_unparseable_versions_never_defer(env):
    core, transport, clock = env
    runtime(core, transport, clock, 'dev').collect(timeout=1)
    assert core.collect()['status'] == 'ok'
    runtime(core, transport, clock, 'dev').collect(timeout=1)
    core.runtime_version = 'dev'
    assert core.collect()['status'] == 'ok'


def test_a_runtime_without_the_record_is_noticed(env):
    core, transport, clock = env
    client, _ = bind(core, transport)
    core.collect()
    clock.advance(5)
    # A runtime from before the record updates last_intake only.
    with sqlite3.connect(core.state_dir / 'state.sqlite3') as db:
        db.execute('UPDATE account SET last_intake=? WHERE singleton=1', (clock.now,))
    legacy = clock.now
    clock.advance(5)
    core.collect()
    assert core.collector_status()['legacy_intake_at'] == legacy
    text = human_status({'label': 'Alpha', **core.connection_status(client)})
    assert 'older than 0.9.24 fetched from Telegram' in text
    clock.advance(7 * 86400)
    assert core.collector_status()['legacy_intake_at'] is None


def test_status_says_when_this_runtime_stands_aside(env):
    core, transport, clock = env
    client, _ = bind(core, transport)
    runtime(core, transport, clock, '99.0.0').collect(judge=True)
    status = core.connection_status(client)
    assert status['collector']['deferring_to'] == '99.0.0'
    assert 'a newer runtime (99.0.0) fetches from Telegram' in human_status({'label': 'Alpha', **status})


def test_a_newer_one_shot_poll_never_silences_an_older_long_poller(env):
    core, transport, clock = env
    client, _ = bind(core, transport)
    core.collect(timeout=1)
    newer = runtime(core, transport, clock, '99.0.0')
    # A hook or messages_poll on a newer runtime collects but claims nothing.
    assert newer.collect()['status'] == 'ok'
    assert core.collector_status()['collector_version'] == core.runtime_version
    append(transport, 'still routed')
    assert core.collect(timeout=1)['status'] == 'ok'
    assert [d['text'] for d in core.poll(client)['deliveries']] == ['still routed']
    # It is not mistaken for a runtime that predates the record either.
    assert core.collector_status()['legacy_intake_at'] is None


def test_a_clock_that_went_backwards_does_not_keep_the_record_fresh(env):
    core, transport, clock = env
    runtime(core, transport, clock, '99.0.0').collect(timeout=1)
    clock.now -= 3600
    assert core.collect()['status'] == 'ok'


def test_prerelease_suffixes_count_as_the_release():
    from agents_over_telegram.core import _version_key
    assert _version_key('0.9.24rc1') == _version_key('0.9.24') == (0, 9, 24)
    assert _version_key('0.9.10') > _version_key('0.9.9')
