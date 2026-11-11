"""The always-on collector's health: a watchdog for a stuck loop, and warnings when it stopped fetching."""
from agents_over_telegram import serve
from agents_over_telegram.credentials import write_private_json
from agents_over_telegram.onboarding import human_status
from agents_over_telegram.push import hook_response
from test_core import bind, env  # noqa: F401 (env is a fixture)


class Ticks:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_the_watchdog_notices_a_loop_without_progress():
    ticks = Ticks()
    watchdog = serve.Watchdog(limit=300, clock=ticks)
    ticks.now = 299
    assert not watchdog.stuck()
    ticks.now = 301
    assert watchdog.stuck()
    watchdog.beat()
    assert not watchdog.stuck()


def test_the_watchdog_exits_the_process_when_stuck():
    ticks = Ticks()
    watchdog = serve.Watchdog(limit=10, clock=ticks)
    exits, logs = [], []

    def sleep(seconds):
        ticks.now += seconds

    thread = watchdog.start(logs.append, exit_=exits.append, sleep=sleep)
    thread.join(timeout=5)
    assert exits == [serve.STUCK_EXIT] and 'no progress' in logs[0]


def test_serve_beats_once_per_iteration(env, monkeypatch):
    core, transport, clock = env
    monkeypatch.setattr(core, 'collect', lambda **kw: {'status': 'ok'})
    monkeypatch.setattr(core, 'flush', lambda **kw: None)
    beats = []

    class Recorder:
        def start(self, log):
            beats.append('start')

        def beat(self):
            beats.append('beat')
    left = [3]

    def keep_running():
        left[0] -= 1
        return left[0] >= 0
    serve.run(core, core.state_dir, poll_timeout=0, sleep=lambda s: None, keep_running=keep_running,
              log=lambda t: None, watchdog=Recorder())
    assert beats == ['start', 'beat', 'beat', 'beat']


def install(core):
    write_private_json(core.state_dir / serve.POINTER, {'version': 1, 'executable': str(serve.own_executable())})


def test_no_warning_without_the_service(env):
    core, transport, clock = env
    client, _ = bind(core, transport)
    clock.advance(3600)
    status = core.connection_status(client)['collector']
    assert status['service_installed'] is False and status['service_stale'] is False
    assert hook_response(core, client, 'UserPromptSubmit', {}) is None


def test_a_silent_service_is_reported_in_status_and_prompts(env):
    core, transport, clock = env
    client, _ = bind(core, transport)
    install(core)
    core.collect()
    clock.advance(core.SERVICE_STALE - 1)
    assert core.collector_status()['service_stale'] is False
    assert hook_response(core, client, 'UserPromptSubmit', {}) is None
    clock.advance(2)
    status = core.connection_status(client)
    assert status['collector']['service_stale'] is True
    assert 'has not tried to fetch from Telegram since' in human_status({'label': 'Alpha', **status})
    answer = hook_response(core, client, 'UserPromptSubmit', {})
    assert 'aot serve --status' in answer['hookSpecificOutput']['additionalContext']
    # Only prompts and session starts carry it; tool calls stay quiet.
    assert hook_response(core, client, 'PostToolUse', {'tool_name': 'Bash'}) is None
    core.collect()
    assert hook_response(core, client, 'UserPromptSubmit', {}) is None
