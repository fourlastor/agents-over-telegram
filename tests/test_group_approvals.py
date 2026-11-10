"""Owner approvals asked in the bound group as an ephemeral message only the owner sees."""
import json

import pytest

from agents_over_telegram.transport import TransportError
from test_core import bind, env  # noqa: F401 (env is a fixture)
from test_owner_approval import OWNER, ask

GROUP = -100


@pytest.fixture
def owner(env):
    core, transport, clock = env
    client, _ = bind(core, transport)
    assert core.delivery_settings(client, approvals_in='group')['approvals_in'] == 'group'
    return core, transport, clock, client


def group_press(transport, approval_id, ephemeral_id, choice='a', sender=OWNER, receiver=OWNER, chat=GROUP):
    number = max((u['update_id'] for u in transport.updates), default=0) + 1
    transport.updates.append({'update_id': number, 'callback_query': {
        'id': f'cb{number}', 'from': {'id': sender, 'is_bot': False},
        'message': {'message_id': 0, 'ephemeral_message_id': ephemeral_id, 'receiver_user': {'id': receiver},
                    'chat': {'id': chat, 'type': 'supergroup'}},
        'data': f'aot:{choice}:{approval_id}'}})
    return f'cb{number}'


def test_private_is_the_default(env):
    core, transport, clock = env
    client, _ = bind(core, transport)
    assert core.delivery_settings(client)['approvals_in'] == 'private'
    assert core.connection_status(client)['routing']['approvals_in'] == 'private'
    with pytest.raises(Exception, match='invalid_delivery_settings'):
        core.delivery_settings(client, approvals_in='everyone')


def test_the_request_goes_to_the_group_only_for_the_owner(owner):
    core, transport, clock, client = owner
    result = ask(core, client)
    assert result['where'] == 'group' and result['delivery'] == 'sent'
    message = transport.sent[-1]
    assert message['chat']['id'] == GROUP and message['receiver_user'] == {'id': OWNER}
    assert message['message_id'] == 0 and message['ephemeral_message_id'] > 0
    assert [b['text'] for b in message['reply_markup']['inline_keyboard'][0]] == ['Approve', 'Decline']
    assert core.delivery_settings(client)['auto_allow'] is False


def test_the_owners_press_in_the_group_applies_and_edits_the_ephemeral_message(owner):
    core, transport, clock, client = owner
    approval = ask(core, client)['approval_id']
    ephemeral_id = transport.sent[-1]['ephemeral_message_id']
    callback = group_press(transport, approval, ephemeral_id)
    core.collect()
    assert core.delivery_settings(client)['auto_allow'] is True
    assert transport.callback_answers[-1] == {'id': callback, 'text': 'Approved.'}
    edit = transport.edits[-1]
    assert (edit['chat_id'], edit['receiver_user_id'], edit['ephemeral_message_id']) == (GROUP, OWNER, ephemeral_id)
    assert edit['text'].endswith('Approved and applied.')
    outcome = core.poll(client)['deliveries'][-1]
    assert outcome['kind'] == 'owner_approval' and outcome['outcome'] == 'approved'


@pytest.mark.parametrize('case', ['stranger', 'other_receiver', 'other_message', 'other_chat', 'private_chat'])
def test_presses_that_are_not_the_owners_on_this_message_are_refused(owner, case):
    core, transport, clock, client = owner
    approval = ask(core, client)['approval_id']
    ephemeral_id = transport.sent[-1]['ephemeral_message_id']
    if case == 'stranger':
        group_press(transport, approval, ephemeral_id, sender=202, receiver=202)
    elif case == 'other_receiver':
        group_press(transport, approval, ephemeral_id, receiver=202)
    elif case == 'other_message':
        group_press(transport, approval, ephemeral_id + 7)
    elif case == 'other_chat':
        group_press(transport, approval, ephemeral_id, chat=-200)
    else:
        from test_owner_approval import press
        press(transport, approval)
    core.collect()
    assert core.delivery_settings(client)['auto_allow'] is False
    assert transport.callback_answers[-1]['text'] == 'This button does not belong to a request of yours.'


@pytest.mark.parametrize('failure', [TransportError(400), RuntimeError('boom'), 'no_ephemeral_id'])
def test_a_failed_group_send_falls_back_to_the_private_chat(owner, monkeypatch, failure):
    core, transport, clock, client = owner
    original = transport.send_message

    def send(chat_id, text, thread_id=None, reply_to=None, reply_markup=None, ephemeral_to=None):
        if ephemeral_to is not None:
            if failure == 'no_ephemeral_id':
                return {'message_id': 0, 'chat': {'id': chat_id}}
            raise failure
        return original(chat_id, text, thread_id=thread_id, reply_to=reply_to, reply_markup=reply_markup)
    monkeypatch.setattr(transport, 'send_message', send)
    result = ask(core, client)
    assert result['where'] == 'private'
    assert transport.sent[-1]['chat']['id'] == OWNER
    # The private press works; a group press for it does not.
    from test_owner_approval import press
    press(transport, result['approval_id'])
    core.collect()
    assert core.delivery_settings(client)['auto_allow'] is True


def test_the_real_transport_sends_and_edits_ephemeral_messages():
    from agents_over_telegram.transport import TelegramTransport
    captured = []

    class Response:
        def __init__(self, body):
            self._body = body

        def read(self, limit=None):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class Opener:
        def open(self, request, timeout=0):
            method = request.full_url.rsplit('/', 1)[-1]
            captured.append((method, json.loads(request.data)))
            result = True if method.startswith('edit') else {'message_id': 0, 'ephemeral_message_id': 5,
                                                             'chat': {'id': GROUP}}
            return Response(json.dumps({'ok': True, 'result': result}).encode())

    transport = TelegramTransport('123456:TEST_TOKEN_abcdefghijklmnop', opener=Opener())
    transport.send_message(GROUP, 'Approve?', thread_id=7, ephemeral_to=OWNER)
    transport.edit_ephemeral_message_text(GROUP, OWNER, 5, 'Approved.')
    assert captured[0] == ('sendMessage', {'chat_id': GROUP, 'text': 'Approve?', 'message_thread_id': 7,
                                           'link_preview_options': {'is_disabled': True},
                                           'ephemeral_message_parameters': {'receiver_user_id': OWNER}})
    assert captured[1] == ('editEphemeralMessageText', {'chat_id': GROUP, 'receiver_user_id': OWNER,
                                                        'ephemeral_message_id': 5, 'text': 'Approved.',
                                                        'link_preview_options': {'is_disabled': True}})


def test_the_cli_sets_where_approvals_go(owner):
    from agents_over_telegram.cli import parser
    args = parser().parse_args(['delivery-settings', '--client-file', 'c.json', '--approvals-in', 'group'])
    assert args.approvals_in == 'group'
