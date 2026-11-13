"""Answer buttons on owner_notify, and approval outcomes told to the agent."""
import pytest

from agents_over_telegram.core import CoreError
from agents_over_telegram.mcp_server import TOOLS
from test_core import append, bind, env  # noqa: F401 (env is a fixture)
from test_owner_approval import OWNER, ask, press

QUESTION = 'Deploy 0.9.20 now?'


@pytest.fixture
def owner(env):
    core, transport, clock = env
    client, _ = bind(core, transport)
    core.delivery_settings(client, owner_notify=True)
    return core, transport, clock, client


def notify(core, client, choices=('Yes', 'No'), key='q1'):
    return core.owner_notify(client, QUESTION, key, choices=list(choices) if choices else None)


def tap(transport, status, index, sender=OWNER, message_id=None):
    data = f'aot:c:{status["notify_id"].removeprefix("owner_")}:{index}'
    callback = press(transport, None, sender=sender, data=data)
    if message_id is not None or status.get('message_id') is not None:
        transport.updates[-1]['callback_query']['message']['message_id'] = (
            message_id if message_id is not None else status['message_id'])
    return callback


def deliveries(core, client):
    result = core.poll(client)
    core.ack(client, [d['delivery_id'] for d in result['deliveries']], result['lease_token'])
    return result['deliveries']


def test_a_notice_with_choices_has_one_button_per_label(owner):
    core, transport, clock, client = owner
    status = notify(core, client, ('Yes', 'No', 'Later'))
    message = transport.sent[-1]
    assert message['chat']['id'] == OWNER
    assert message['text'].endswith('\nTap an answer, or reply to this message to answer the agent.')
    rows = message['reply_markup']['inline_keyboard']
    assert [row[0]['text'] for row in rows] == ['Yes', 'No', 'Later']
    assert all(len(row[0]['callback_data'].encode()) <= 64 for row in rows)
    assert status['choices'] == ['Yes', 'No', 'Later'] and status['choice'] is None
    assert status['choice_expires_at'] == clock.now + core.OWNER_CHOICE_SECONDS


def test_a_notice_without_choices_is_unchanged(owner):
    core, transport, clock, client = owner
    status = notify(core, client, None)
    assert 'reply_markup' not in transport.sent[-1]
    assert transport.sent[-1]['text'].endswith('\nReply to this message to answer the agent.')
    assert 'choices' not in status


def test_the_owners_press_is_delivered_once_and_closes_the_buttons(owner):
    core, transport, clock, client = owner
    status = notify(core, client)
    callback = tap(transport, status, 0)
    core.collect()
    [delivery] = deliveries(core, client)
    assert delivery['kind'] == 'owner_choice' and delivery['choice'] == 'Yes' and delivery['choice_index'] == 0
    assert delivery['owner_notice'] is True and delivery['addressed'] is True
    assert delivery['notify_id'] == status['notify_id'] and delivery['notice_message_id'] == status['message_id']
    assert delivery['sender']['id'] == OWNER
    assert transport.callback_answers[-1] == {'id': callback, 'text': 'Answered: Yes'}
    assert transport.edits[-1]['text'].endswith('\n\nAnswered: Yes')
    assert core.delivery_status(client, status['notify_id'])['choice'] == 'Yes'
    # A second press, even on the other button, changes nothing.
    second = tap(transport, status, 1)
    core.collect()
    assert core.poll(client)['deliveries'] == []
    assert transport.callback_answers[-1] == {'id': second, 'text': 'Already answered: Yes.'}


def test_the_owners_answer_reaches_an_agent_with_a_full_queue(owner):
    core, transport, clock, client = owner
    core.queue_limit = 1
    status = notify(core, client)
    append(transport, 'fills the queue')
    core.collect()
    tap(transport, status, 1)
    core.collect()
    # The owner was told "Answered: No", so the agent gets it despite the bound.
    assert [d.get('choice') for d in deliveries(core, client)] == [None, 'No']


def test_a_free_text_reply_still_works_and_keeps_the_buttons(owner):
    core, transport, clock, client = owner
    status = notify(core, client)
    number = max((u['update_id'] for u in transport.updates), default=0) + 1
    transport.updates.append({'update_id': number, 'message': {
        'message_id': 500, 'date': int(clock.now), 'chat': {'id': OWNER, 'type': 'private'},
        'from': {'id': OWNER, 'is_bot': False}, 'text': 'Not yet, wait for CI',
        'reply_to_message': {'message_id': status['message_id'], 'from': {'id': 9999, 'is_bot': True}}}})
    core.collect()
    [reply] = deliveries(core, client)
    assert reply['kind'] == 'message' and reply['text'] == 'Not yet, wait for CI' and reply['owner_notice'] is True
    # The buttons still answer afterwards.
    tap(transport, status, 1)
    core.collect()
    [choice] = deliveries(core, client)
    assert choice['choice'] == 'No'


@pytest.mark.parametrize('case', ['stranger', 'other_message', 'bad_index', 'unknown_notice'])
def test_presses_that_are_not_the_owners_on_this_notice_are_refused(owner, case):
    core, transport, clock, client = owner
    status = notify(core, client)
    if case == 'stranger':
        tap(transport, status, 0, sender=202)
    elif case == 'other_message':
        tap(transport, status, 0, message_id=status['message_id'] + 50)
    elif case == 'bad_index':
        tap(transport, status, 3)
    else:
        tap(transport, {'notify_id': 'owner_' + '0' * 24, 'message_id': status['message_id']}, 0)
    core.collect()
    assert core.poll(client)['deliveries'] == []
    assert transport.callback_answers[-1]['text'] == 'This button does not belong to a question of yours.'
    assert core.delivery_status(client, status['notify_id'])['choice'] is None


def test_expired_buttons_point_to_a_reply(owner):
    core, transport, clock, client = owner
    status = notify(core, client)
    clock.advance(core.OWNER_CHOICE_SECONDS + 1)
    tap(transport, status, 0)
    core.collect()
    assert core.poll(client)['deliveries'] == []
    assert transport.callback_answers[-1]['text'] == 'This question expired. Reply to the message instead.'
    assert transport.edits[-1]['text'].endswith('The buttons expired.')


@pytest.mark.parametrize('choices', [['Yes'], ['a', 'b', 'c', 'd', 'e'], ['Yes', 'Yes'], ['Yes', ''],
                                     ['Yes', ' No'], ['Yes', 'x' * 33], ['Yes', 'line\nbreak'], 'Yes', ['Yes', 3]])
def test_choices_are_validated_before_anything_is_sent(owner, choices):
    core, transport, clock, client = owner
    sent = len(transport.sent)
    with pytest.raises(CoreError, match='invalid_choices'):
        core.owner_notify(client, QUESTION, 'bad', choices=choices)
    assert len(transport.sent) == sent


def test_idempotency_covers_the_choices(owner):
    core, transport, clock, client = owner
    first = notify(core, client)
    assert notify(core, client)['notify_id'] == first['notify_id']
    with pytest.raises(CoreError, match='idempotency_conflict'):
        notify(core, client, ('Yes', 'Maybe'))


def test_the_mcp_tool_takes_choices():
    [tool] = [tool for tool in TOOLS if tool.name == 'owner_notify']
    schema = tool.inputSchema['properties']['choices']
    assert (schema['minItems'], schema['maxItems'], schema['items']['maxLength']) == (2, 4, 32)


@pytest.mark.parametrize('button,outcome', [('a', 'approved'), ('d', 'declined')])
def test_the_agent_hears_how_the_owner_answered_an_approval(owner, button, outcome):
    core, transport, clock, client = owner
    approval = ask(core, client)['approval_id']
    press(transport, approval, button)
    core.collect()
    [delivery] = deliveries(core, client)
    assert delivery['kind'] == 'owner_approval' and delivery['outcome'] == outcome
    assert delivery['approval_id'] == approval and delivery['action'] == 'delivery_settings'
    assert delivery['params'] == {'auto_allow': True} and delivery['error'] is None
    assert delivery['owner_notice'] is True


def test_the_agent_hears_when_a_request_expires_unanswered(owner):
    core, transport, clock, client = owner
    approval = ask(core, client)['approval_id']
    clock.advance(core.APPROVAL_SECONDS + 1)
    core.collect()
    [expired] = deliveries(core, client)
    assert expired['outcome'] == 'expired' and expired['approval_id'] == approval and expired['sender'] is None
    # A late press only gets a toast; the agent is not told twice.
    press(transport, approval)
    core.collect()
    assert core.poll(client)['deliveries'] == []
    assert transport.callback_answers[-1]['text'] == 'This request was already expired.'


def test_the_agent_hears_when_an_approved_change_cannot_apply(owner, monkeypatch):
    core, transport, clock, client = owner
    approval = ask(core, client)['approval_id']

    def refuse(*args, **kwargs):
        raise CoreError('binding_changed')
    monkeypatch.setattr(core, '_write_delivery_settings', refuse)
    press(transport, approval)
    core.collect()
    [failed] = deliveries(core, client)
    assert (failed['outcome'], failed['error']) == ('apply_failed', 'binding_changed')


def test_replying_to_a_notice_the_owner_answered_counts_as_answering_the_owner(owner):
    core, transport, clock, client = owner
    status = notify(core, client)
    tap(transport, status, 0)
    core.collect()
    deliveries(core, client)
    answer = core.owner_notify(client, 'Deploying now.', 'a1', reply_to=status['message_id'])
    assert answer['answers_owner'] is True


def test_owner_answers_and_expiries_wake_at_once():
    from agents_over_telegram.push import _wakes_at_once
    binding = {'owner_id': OWNER}
    owner = {'id': OWNER, 'is_bot': False}
    exempt = {'addressed', 'owner'}
    assert _wakes_at_once({'kind': 'owner_choice', 'sender': owner}, binding, exempt)
    assert _wakes_at_once({'kind': 'owner_approval', 'sender': owner, 'outcome': 'declined'}, binding, exempt)
    assert _wakes_at_once({'kind': 'owner_approval', 'sender': None, 'outcome': 'expired'}, binding, exempt)
    assert not _wakes_at_once({'kind': 'owner_approval', 'sender': None, 'outcome': 'expired'}, binding, {'addressed'})
