"""Offline proof for the optional model-judged wake decision (judge.py)."""
import json
import os
import stat
import types

import anthropic
import pytest

from agents_over_telegram import judge as judge_module
from agents_over_telegram.cli import parser, run
from agents_over_telegram.credentials import CredentialError
from agents_over_telegram.judge import AnthropicJudge, JudgeUnavailable
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
    assert core.collect()['status'] == 'ok'
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
    core.collect()
    assert claim_new(core, client, 'watch')['new'] == 1
    append(transport, 'and the second part')
    core.collect()
    # Inside the wake interval only addressed messages are announced at once.
    assert claim_new(core, client, 'watch')['new'] == 1


def test_a_no_or_a_failure_keeps_the_message_as_context(judged):
    core, transport, clock, client = judged
    core._judge = FakeJudge(False, JudgeUnavailable('judge_unreachable'), RuntimeError('boom'))
    append(transport, 'talking to someone else')
    append(transport, 'more chatter')
    append(transport, 'and more')
    assert core.collect()['status'] == 'ok'
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
    core.collect()
    assert judge.calls == [] and queue(core) == []
    assert [d['text'] for d in deliveries(core, client)] == [f'@{transport.username} addressed already']


def test_nothing_is_judged_unless_the_owner_turns_it_on(env):
    core, transport, clock = env
    client, _ = bind(core, transport)
    core.delivery_settings(client, delivery_filter='addressed')
    core._judge = judge = FakeJudge(True)
    append(transport, 'plain message')
    core.collect()
    assert judge.calls == [] and queue(core) == []
    assert core.delivery_settings(client)['model_judge'] is False
    assert core.connection_status(client)['routing']['model_judge'] is False


def test_without_a_key_nothing_is_sent_and_the_message_stays_context(judged):
    core, transport, clock, client = judged
    core._judge = None
    append(transport, 'plain message')
    core.collect()
    assert queue(core) == [('no_key', None)]
    assert deliveries(core, client) == []


def test_the_default_judge_loads_only_from_the_protected_key_file(judged):
    core, transport, clock, client = judged
    core._judge = core.__class__.__init__.__kwdefaults__['judge']
    assert core._judge_ready() is None
    judge_module.store_key(core.state_dir, 'sk-test-key')
    loaded = core._judge_ready()
    assert isinstance(loaded, AnthropicJudge) and loaded.model == 'claude-haiku-5-5'
    assert 'sk-test-key' not in repr(loaded)


def test_the_model_sees_recent_messages_and_the_agents_own_posts_in_order(judged):
    core, transport, clock, client = judged
    core._judge = judge = FakeJudge(False, False)
    append(transport, 'what should we do next?')
    core.collect()
    clock.advance(5)
    core.send(client, 'Shall I update the docs?', 'own-1')
    core.flush(client)
    clock.advance(5)
    append(transport, 'yes please')
    append(transport, 'later message')
    core.collect()
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
    core.collect()
    assert len(judge.calls) == core.JUDGE_PER_COLLECT
    clock.advance(core.JUDGE_MAX_AGE + 1)
    core.collect()
    assert len(judge.calls) == core.JUDGE_PER_COLLECT
    assert [status for status, _ in queue(core)].count('expired') == 2
    monkeypatch.setattr(core, 'JUDGE_LIMITS', ((3, 3600),))
    append(transport, 'over the cap')
    core.collect()
    assert len(judge.calls) == core.JUDGE_PER_COLLECT and queue(core)[-1] == ('limited', None)


def test_a_setting_changed_before_judging_skips_the_message(judged):
    core, transport, clock, client = judged
    core._judge = judge = FakeJudge(True)
    calls = []
    original = core._judge_pending
    core._judge_pending = lambda: calls.append(1)
    append(transport, 'plain message')
    core.collect()
    core._judge_pending = original
    core.delivery_settings(client, delivery_filter='all')
    core.collect()
    assert judge.calls == [] and queue(core) == [('skipped', None)]


def test_a_full_queue_keeps_the_message_as_context(judged, monkeypatch):
    core, transport, clock, client = judged
    core._judge = FakeJudge(True)
    monkeypatch.setattr(core, 'queue_limit', 1)
    append(transport, f'@{transport.username} fills the queue')
    append(transport, 'judged yes')
    core.collect()
    assert [d['text'] for d in deliveries(core, client)] == [f'@{transport.username} fills the queue']
    assert queue(core) == [('yes', 'queue_full')]


# The key file and the Anthropic call.

def test_the_key_is_stored_privately_and_never_reported(tmp_path):
    state = tmp_path / 'state'
    assert judge_module.key_status(state) == {'configured': False, 'model': None}
    judge_module.store_key(state, ' sk-secret \n')
    path = judge_module.judge_path(state)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert json.loads(path.read_text())['api_key'] == 'sk-secret'
    assert judge_module.key_status(state) == {'configured': True, 'model': 'claude-haiku-5-5'}
    judge_module.store_key(state, 'sk-other', 'claude-sonnet-5-5')
    assert judge_module.key_status(state)['model'] == 'claude-sonnet-5-5'
    with pytest.raises(CredentialError, match='invalid_judge_key'):
        judge_module.store_key(state, 'two words')
    with pytest.raises(CredentialError, match='invalid_judge_model'):
        judge_module.store_key(state, 'sk-x', 'Bad Model!')
    assert judge_module.clear_key(state) is True and judge_module.load(state) is None
    assert judge_module.clear_key(state) is False


def test_a_key_file_others_can_read_is_ignored(tmp_path):
    state = tmp_path / 'state'
    judge_module.store_key(state, 'sk-secret')
    os.chmod(judge_module.judge_path(state), 0o644)
    assert judge_module.load(state) is None
    assert judge_module.key_status(state)['configured'] is False


class FakeMessages:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def create(self, **request):
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def response(text, stop_reason='end_turn'):
    blocks = [types.SimpleNamespace(type='thinking', thinking=''),
              types.SimpleNamespace(type='text', text=text)]
    return types.SimpleNamespace(stop_reason=stop_reason, content=blocks)


def ask(result):
    judge = AnthropicJudge('sk-test')
    messages = FakeMessages(result)
    judge._client = types.SimpleNamespace(messages=messages)
    verdict = judge(agent_label='Alpha', bot_username='aot_test_bot',
                    history=[{'sender': '@someone', 'text': 'Shall I go on?'}],
                    candidate={'sender': '@owner', 'text': 'yes'})
    return verdict, messages.requests


def test_the_request_asks_a_small_model_for_one_boolean():
    verdict, [request] = ask(response('{"addressed": true}'))
    assert verdict is True
    assert request['model'] == 'claude-haiku-5-5'
    assert request['output_config']['effort'] == 'low'
    assert request['output_config']['format']['schema']['required'] == ['addressed']
    assert 'thinking' not in request and 'temperature' not in request
    prompt = request['messages'][0]['content']
    assert '"Alpha"' in prompt and '@aot_test_bot' in prompt and '- @someone: Shall I go on?' in prompt
    assert '<new_message>\n- @owner: yes\n</new_message>' in prompt
    assert ask(response('{"addressed": false}'))[0] is False


def test_the_client_is_pinned_to_the_anthropic_api(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_BASE_URL', 'https://elsewhere.example')
    client = AnthropicJudge('sk-test')._messages()._client
    assert str(client.base_url).startswith('https://api.anthropic.com')
    assert client.max_retries == 0 and client.timeout == judge_module.TIMEOUT_SECONDS


@pytest.mark.parametrize('result,code', [
    (response('{"addressed": "yes"}'), 'judge_no_answer'),
    (response('not json'), 'judge_no_answer'),
    (response('', stop_reason='max_tokens'), 'judge_no_answer'),
    (response('', stop_reason='refusal'), 'judge_no_answer'),
])
def test_an_unusable_answer_is_no_answer(result, code):
    with pytest.raises(JudgeUnavailable) as error:
        ask(result)
    assert error.value.code == code


def _status_error(cls, status):
    import httpx2
    request = httpx2.Request('POST', 'https://api.anthropic.com/v1/messages')
    return cls('error', response=httpx2.Response(status, request=request), body=None)


@pytest.mark.parametrize('error,code', [
    (lambda: _status_error(anthropic.AuthenticationError, 401), 'judge_key_rejected'),
    (lambda: _status_error(anthropic.RateLimitError, 429), 'judge_rate_limited'),
    (lambda: _status_error(anthropic.InternalServerError, 500), 'judge_api_error'),
    (lambda: anthropic.APITimeoutError(request=__import__('httpx2').Request('POST', 'https://api.anthropic.com')),
     'judge_unreachable'),
])
def test_api_errors_become_codes_without_the_key(error, code):
    with pytest.raises(JudgeUnavailable) as raised:
        ask(error())
    assert raised.value.code == code and 'sk-test' not in str(raised.value)


# The owner's CLI.

def test_cli_reports_the_key_without_printing_it(tmp_path, monkeypatch):
    state = tmp_path / 'state'
    judge_module.store_key(state, 'sk-cli-secret')

    def cli(*args):
        return run(parser().parse_args(['--state-dir', str(state), '--fake', *args]))

    status = cli('judge-key', 'status')
    assert status == {'configured': True, 'model': 'claude-haiku-5-5'} and 'sk-cli-secret' not in json.dumps(status)
    monkeypatch.setattr('sys.stdin', types.SimpleNamespace(isatty=lambda: False))
    with pytest.raises(CredentialError, match='private_terminal_required'):
        cli('judge-key', 'set')
    with pytest.raises(CredentialError, match='judge_model_only_with_set'):
        cli('judge-key', 'status', '--model', 'claude-haiku-5-5')
    assert cli('judge-key', 'clear') == {'cleared': True}


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
