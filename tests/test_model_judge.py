"""Offline proof for the optional model-judged wake decision (judge.py)."""
import http.server
import json
import os
import stat
import threading
import types

import pytest

from agents_over_telegram import judge as judge_module
from agents_over_telegram.cli import parser, run
from agents_over_telegram.credentials import CredentialError
from agents_over_telegram.judge import DecisionJudge, JudgeUnavailable
from agents_over_telegram.push import claim_new, ensure_tables
from test_core import append, bind, env, rows  # noqa: F401 (env is a fixture)


class FakeJudge:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, **context):
        self.calls.append(context)
        answer = self.answers.pop(0) if self.answers else False
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def judged(env):
    core, transport, clock = env
    client, _ = bind(core, transport)
    core.allow_sender(client, 202, is_bot=True)
    core.delivery_settings(client, delivery_filter='addressed', model_judge=True)
    core._judge = FakeJudge()
    return core, transport, clock, client


def deliveries(core, client):
    return core.poll(client)['deliveries']


def queue(core):
    return rows(core, 'SELECT status,error FROM judge_queue ORDER BY created,rowid')


def test_a_yes_delivers_the_message_as_addressed_by_the_model(judged):
    core, transport, clock, client = judged
    core._judge = FakeJudge(True)
    append(transport, 'sounds good, go ahead')
    assert core.collect(judge=True)['status'] == 'ok'
    [delivery] = deliveries(core, client)
    assert delivery['text'] == 'sounds good, go ahead'
    assert delivery['addressed'] is True and delivery['addressed_by'] == 'model'
    assert 'context_reason' not in delivery
    assert queue(core) == [('yes', None)]
    assert rows(core, 'SELECT count(*) FROM inbox WHERE context=1') == [(0,)]


def test_a_promoted_message_is_announced_at_once_like_other_addressed_messages(judged):
    core, transport, clock, client = judged
    core.delivery_settings(client, wake_interval=600)
    core._judge = FakeJudge(True)
    ensure_tables(core)
    append(transport, f'@{transport.username} first')
    core.collect(judge=True)
    assert claim_new(core, client, 'watch')['new'] == 1
    append(transport, 'and the second part')
    core.collect(judge=True)
    # Inside the wake interval only addressed messages are announced at once.
    assert claim_new(core, client, 'watch')['new'] == 1


def test_a_no_or_a_failure_keeps_the_message_as_context(judged):
    core, transport, clock, client = judged
    core._judge = FakeJudge(False, JudgeUnavailable('judge_unreachable'), RuntimeError('boom'))
    append(transport, 'talking to someone else')
    append(transport, 'more chatter')
    append(transport, 'and more')
    assert core.collect(judge=True)['status'] == 'ok'
    assert deliveries(core, client) == []
    assert queue(core) == [('no', None), ('failed', 'judge_unreachable'), ('failed', 'judge_failed')]
    assert [m['status'] for m in core.history(client, limit=10)['messages']] == ['context'] * 3


def test_only_a_persons_new_unaddressed_message_is_judged(judged):
    core, transport, clock, client = judged
    core._judge = judge = FakeJudge()
    append(transport, f'@{transport.username} addressed already')
    append(transport, 'from a peer agent', sender=202, bot=True)
    append(transport, 'from a stranger', sender=303)
    append(transport, '')
    core.collect(judge=True)
    assert judge.calls == [] and queue(core) == []
    assert [d['text'] for d in deliveries(core, client)] == [f'@{transport.username} addressed already']


def test_nothing_is_judged_unless_the_owner_turns_it_on(env):
    core, transport, clock = env
    client, _ = bind(core, transport)
    core.delivery_settings(client, delivery_filter='addressed')
    core._judge = judge = FakeJudge(True)
    append(transport, 'plain message')
    core.collect(judge=True)
    assert judge.calls == [] and queue(core) == []
    assert core.delivery_settings(client)['model_judge'] is False
    assert core.connection_status(client)['routing']['model_judge'] is False


def test_without_a_configured_model_nothing_is_sent_and_the_message_stays_context(judged):
    core, transport, clock, client = judged
    core._judge = None
    append(transport, 'plain message')
    core.collect(judge=True)
    assert queue(core) == [('no_model', None)]
    assert deliveries(core, client) == []


def test_the_default_judge_loads_only_from_the_protected_config_file(judged):
    core, transport, clock, client = judged
    core._judge = core.__class__.__init__.__kwdefaults__['judge']
    assert core._judge_ready() is None
    judge_module.store_config(core.state_dir, 'https://api.typesafe.ai/v1/systemone', 'jev-latest', 'tk-test-key')
    loaded = core._judge_ready()
    assert isinstance(loaded, DecisionJudge) and loaded.model == 'jev-latest'
    assert 'tk-test-key' not in repr(loaded)


def test_the_model_sees_recent_messages_and_the_agents_own_posts_in_order(judged):
    core, transport, clock, client = judged
    core._judge = judge = FakeJudge(False, False)
    append(transport, 'what should we do next?')
    core.collect(judge=True)
    clock.advance(5)
    core.send(client, 'Shall I update the docs?', 'own-1')
    core.flush(client)
    clock.advance(5)
    append(transport, 'yes please')
    append(transport, 'later message')
    core.collect(judge=True)
    context = judge.calls[1]
    assert context['agent_label'] == 'Alpha' and context['bot_username'] == transport.username
    assert context['candidate'] == {'sender': '@participant', 'text': 'yes please'}
    assert context['history'] == [
        {'sender': '@participant', 'text': 'what should we do next?'},
        {'sender': f'this agent (Alpha, @{transport.username})',
         'text': f'[Agent: Alpha | shared bot @{transport.username}]\nShall I update the docs?'}]


def test_judging_is_bounded_per_collection_by_age_and_by_rolling_caps(judged, monkeypatch):
    core, transport, clock, client = judged
    core._judge = judge = FakeJudge()
    for number in range(5):
        append(transport, f'chatter {number}')
    core.collect(judge=True)
    assert len(judge.calls) == core.JUDGE_PER_COLLECT
    clock.advance(core.JUDGE_MAX_AGE + 1)
    core.collect(judge=True)
    assert len(judge.calls) == core.JUDGE_PER_COLLECT
    assert [status for status, _ in queue(core)].count('expired') == 2
    monkeypatch.setattr(core, 'JUDGE_LIMITS', ((3, 3600),))
    append(transport, 'over the cap')
    core.collect(judge=True)
    assert len(judge.calls) == core.JUDGE_PER_COLLECT and queue(core)[-1] == ('limited', None)


def test_hooks_and_tool_calls_never_wait_on_the_model(judged):
    core, transport, clock, client = judged
    core._judge = judge = FakeJudge(True)
    append(transport, 'plain message')
    assert core.collect()['status'] == 'ok'
    assert judge.calls == [] and queue(core) == [('queued', None)]
    # The next long-running collection judges it.
    core.collect(judge=True)
    assert queue(core) == [('yes', None)] and len(deliveries(core, client)) == 1


def test_no_call_starts_that_could_overrun_the_time_budget(judged, monkeypatch):
    import time as real_time
    from agents_over_telegram import core as core_module
    core, transport, clock, client = judged
    elapsed = [0.0]

    class SlowJudge(FakeJudge):
        def __call__(self, **context):
            elapsed[0] += 7.0  # each call takes 7 of the 8 seconds allowed
            return super().__call__(**context)

    monkeypatch.setattr(core_module, 'time', types.SimpleNamespace(
        monotonic=lambda: elapsed[0], time=real_time.time, sleep=real_time.sleep))
    core._judge = judge = SlowJudge()
    for number in range(3):
        append(transport, f'chatter {number}')
    core.collect(judge=True)
    # 0 s and 7 s leave room for an 8 s call within 20 s; 14 s does not.
    assert len(judge.calls) == 2
    assert [status for status, _ in queue(core)] == ['no', 'no', 'queued']


def test_a_setting_changed_before_judging_skips_the_message(judged):
    core, transport, clock, client = judged
    core._judge = judge = FakeJudge(True)
    calls = []
    original = core._judge_pending
    core._judge_pending = lambda: calls.append(1)
    append(transport, 'plain message')
    core.collect(judge=True)
    core._judge_pending = original
    core.delivery_settings(client, delivery_filter='all')
    core.collect(judge=True)
    assert judge.calls == [] and queue(core) == [('skipped', None)]


def test_a_full_queue_keeps_the_message_as_context(judged, monkeypatch):
    core, transport, clock, client = judged
    core._judge = FakeJudge(True)
    monkeypatch.setattr(core, 'queue_limit', 1)
    append(transport, f'@{transport.username} fills the queue')
    append(transport, 'judged yes')
    core.collect(judge=True)
    assert [d['text'] for d in deliveries(core, client)] == [f'@{transport.username} fills the queue']
    assert queue(core) == [('yes', 'queue_full')]


# The configuration file.

def test_the_config_is_stored_privately_and_the_key_never_reported(tmp_path):
    state = tmp_path / 'state'
    assert judge_module.config_status(state) == {'configured': False}
    judge_module.store_config(state, 'https://api.typesafe.ai/v1/systemone', 'jev-latest', ' tk-secret \n')
    path = judge_module.judge_path(state)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert json.loads(path.read_text())['api_key'] == 'tk-secret'
    assert judge_module.config_status(state) == {
        'configured': True, 'endpoint': 'https://api.typesafe.ai/v1/systemone', 'model': 'jev-latest',
        'key': True, 'threshold': 0.5}
    # A self-hosted Clef on this computer needs no key and may use plain http.
    judge_module.store_config(state, 'http://127.0.0.1:8080/v1/systemone', 'clef-flash', threshold=0.7)
    assert judge_module.config_status(state)['key'] is False
    assert judge_module.load(state).threshold == 0.7
    assert judge_module.clear_config(state) is True and judge_module.load(state) is None
    assert judge_module.clear_config(state) is False


@pytest.mark.parametrize('endpoint,model,key,threshold,code', [
    ('http://judge.example/v1/systemone', 'clef', '', 0.5, 'judge_endpoint_needs_https'),
    ('ftp://judge.example/', 'clef', '', 0.5, 'judge_endpoint_needs_https'),
    ('https://user:pw@judge.example/', 'clef', '', 0.5, 'invalid_judge_endpoint'),
    ('https://judge.example/ x', 'clef', '', 0.5, 'invalid_judge_endpoint'),
    ('https://judge.example/', 'two words', '', 0.5, 'invalid_judge_model'),
    ('https://judge.example/', 'clef', 'two words', 0.5, 'invalid_judge_key'),
    ('https://judge.example/', 'clef', '', 0.4, 'invalid_judge_threshold'),
    ('https://judge.example/', 'clef', '', 1.0, 'invalid_judge_threshold'),
])
def test_unsafe_or_invalid_settings_are_refused(tmp_path, endpoint, model, key, threshold, code):
    with pytest.raises(CredentialError, match=code):
        judge_module.store_config(tmp_path / 'state', endpoint, model, key, threshold)


def test_a_config_file_others_can_read_is_ignored(tmp_path):
    state = tmp_path / 'state'
    judge_module.store_config(state, 'https://judge.example/', 'clef', 'tk-secret')
    os.chmod(judge_module.judge_path(state), 0o644)
    assert judge_module.load(state) is None
    assert judge_module.config_status(state)['configured'] is False


# The decision call, against a real HTTP server on this computer.

class Endpoint:
    def __init__(self):
        self.requests = []
        self.reply = (200, {'answers': {'addressed': {'type': 'choice', 'choice': 'yes', 'confidence': 0.9,
                                                      'probabilities': {'yes': 0.9, 'no': 0.1}}}})
        endpoint = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                endpoint.requests.append({'path': self.path, 'headers': dict(self.headers), 'body': json.loads(body)})
                status, reply = endpoint.reply
                data = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
                self.send_response(status)
                if 300 <= status < 400:
                    self.send_header('Location', 'http://127.0.0.1:1/stolen')
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_port}/v1/systemone'
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()


@pytest.fixture
def endpoint():
    server = Endpoint()
    yield server
    server.server.shutdown()
    server.server.server_close()


CONTEXT = {'agent_label': 'Alpha', 'bot_username': 'aot_test_bot',
           'history': [{'sender': '@someone', 'text': 'Shall I go on?'}],
           'candidate': {'sender': '@owner', 'text': 'yes'}}


def test_one_choice_question_goes_to_the_endpoint(endpoint):
    judge = DecisionJudge(endpoint.url, 'clef-flash', 'tk-test')
    assert judge(**CONTEXT) is True
    [request] = endpoint.requests
    assert request['path'] == '/v1/systemone'
    assert request['headers']['Authorization'] == 'Bearer tk-test'
    body = request['body']
    assert body['model'] == 'clef-flash' and set(body) == {'model', 'state', 'questions'}
    question = body['questions']['addressed']
    assert question['type'] == 'choice' and set(question['criteria']) == {'yes', 'no'}
    assert '"Alpha"' in body['state'] and '@aot_test_bot' in body['state']
    assert '- @someone: Shall I go on?' in body['state'] and body['state'].endswith('New message:\n- @owner: yes')


def test_without_a_key_no_authorization_header_is_sent(endpoint):
    DecisionJudge(endpoint.url, 'clef-flash')(**CONTEXT)
    assert 'Authorization' not in endpoint.requests[0]['headers']


@pytest.mark.parametrize('probabilities,threshold,expected', [
    ({'yes': 0.9, 'no': 0.1}, 0.5, True),
    ({'yes': 0.3, 'no': 0.7}, 0.5, False),
    ({'yes': 0.6, 'no': 0.4}, 0.7, False),
    ({'yes': 0.5, 'no': 0.5}, 0.5, True),
])
def test_the_yes_probability_decides_against_the_threshold(endpoint, probabilities, threshold, expected):
    endpoint.reply = (200, {'answers': {'addressed': {'type': 'choice', 'choice': 'yes', 'probabilities': probabilities}}})
    assert DecisionJudge(endpoint.url, 'jev-latest', threshold=threshold)(**CONTEXT) is expected


def test_a_cloudflare_envelope_and_a_bare_choice_are_understood(endpoint):
    endpoint.reply = (200, {'success': True, 'result': {'answers': {'addressed': {'choice': 'no'}}}})
    assert DecisionJudge(endpoint.url, 'clef')(**CONTEXT) is False


@pytest.mark.parametrize('reply,code', [
    ((200, b'not json'), 'judge_no_answer'),
    ((200, {'answers': {}}), 'judge_no_answer'),
    ((200, {'answers': {'addressed': {'choice': 'maybe'}}}), 'judge_no_answer'),
    ((200, {'answers': {'addressed': {'probabilities': {'yes': 'high'}}}}), 'judge_no_answer'),
    ((401, {'detail': 'bad key'}), 'judge_key_rejected'),
    ((429, {'detail': 'slow down'}), 'judge_rate_limited'),
    ((422, {'detail': []}), 'judge_request_rejected'),
    ((529, {'detail': 'overloaded'}), 'judge_api_error'),
    ((302, b''), 'judge_redirect_refused'),
])
def test_unusable_answers_and_errors_become_codes(endpoint, reply, code):
    endpoint.reply = reply
    with pytest.raises(JudgeUnavailable) as raised:
        DecisionJudge(endpoint.url, 'clef', 'tk-test')(**CONTEXT)
    assert raised.value.code == code and 'tk-test' not in str(raised.value)
    assert len(endpoint.requests) == 1  # never retried, never redirected


def test_an_unreachable_endpoint_is_no_answer():
    with pytest.raises(JudgeUnavailable, match='judge_unreachable'):
        DecisionJudge('http://127.0.0.1:1/v1/systemone', 'clef')(**CONTEXT)


# The owner's CLI.

def test_cli_reports_the_config_without_printing_the_key(tmp_path, monkeypatch):
    state = tmp_path / 'state'

    def cli(*args):
        return run(parser().parse_args(['--state-dir', str(state), '--fake', *args]))

    monkeypatch.setattr('getpass.getpass', lambda prompt: 'tk-cli-secret')
    monkeypatch.setattr('sys.stdin', types.SimpleNamespace(isatty=lambda: True))
    status = cli('judge-config', 'set', '--endpoint', 'https://api.typesafe.ai/v1/systemone',
                 '--model', 'jev-latest', '--key')
    assert status['configured'] is True and status['key'] is True
    assert 'tk-cli-secret' not in json.dumps(cli('judge-config', 'status'))
    monkeypatch.setattr('sys.stdin', types.SimpleNamespace(isatty=lambda: False))
    with pytest.raises(CredentialError, match='private_terminal_required'):
        cli('judge-config', 'set', '--endpoint', 'https://judge.example/', '--model', 'clef', '--key')
    assert cli('judge-config', 'set', '--endpoint', 'http://localhost:8080/v1/systemone',
               '--model', 'clef-flash')['key'] is False
    with pytest.raises(CredentialError, match='judge_endpoint_and_model_required'):
        cli('judge-config', 'set', '--model', 'clef')
    with pytest.raises(CredentialError, match='judge_options_only_with_set'):
        cli('judge-config', 'status', '--model', 'clef')
    assert cli('judge-config', 'clear') == {'cleared': True}


def test_turning_the_judge_on_needs_the_owner_at_a_terminal(env, monkeypatch):
    core, transport, clock = env
    client, credentials = bind(core, transport)
    client_file = core.state_dir / 'client.json'
    client_file.write_text(json.dumps(credentials))
    client_file.chmod(0o600)
    args = parser().parse_args(['--state-dir', str(core.state_dir), '--fake', 'delivery-settings',
                                '--client-file', str(client_file), '--model-judge', 'on'])
    monkeypatch.setattr('sys.stdin', types.SimpleNamespace(isatty=lambda: False))
    with pytest.raises(CredentialError, match='operator_confirmation_required'):
        run(args)
    assert core.delivery_settings(client)['model_judge'] is False


def test_a_model_yes_does_not_open_the_follow_up_window(judged):
    core, transport, clock, client = judged
    clock.advance(5000)
    now = int(clock.now) - 100  # Telegram dates in the past
    core._judge = FakeJudge(True, False)
    append(transport, 'yes, put it back afterwards', date=now)
    core.collect(judge=True)
    append(transport, 'Daniele, climbing on Thursday?', date=now + 20)
    core.collect(judge=True)
    # The second message is judged on its own, not delivered as a follow-up.
    assert len(core._judge.calls) == 2
    assert [(d['text'], d.get('follow_up')) for d in deliveries(core, client)] == [
        ('yes, put it back afterwards', None)]
    assert queue(core) == [('yes', None), ('no', None)]
