"""Owner-only changes approved with Telegram buttons when no terminal is attached."""
import json
import types

import pytest

from agents_over_telegram.cli import parser, run
from agents_over_telegram.core import CoreError
from agents_over_telegram.transport import TransportError
from test_core import append, bind, env, rows  # noqa: F401 (env is a fixture)

OWNER = 101


def press(transport, approval_id, choice='a', sender=OWNER, chat=None, chat_type='private', data=None):
    """Append one callback_query update for a button press."""
    number = max((u['update_id'] for u in transport.updates), default=0) + 1
    transport.updates.append({'update_id': number, 'callback_query': {
        'id': f'cb{number}', 'from': {'id': sender, 'is_bot': False},
        'message': {'message_id': 77, 'chat': {'id': sender if chat is None else chat, 'type': chat_type}},
        'data': data if data is not None else f'aot:{choice}:{approval_id}'}})
    return f'cb{number}'


def ask(core, client, **changes):
    return core.request_owner_approval(client, 'delivery_settings', changes or {'auto_allow': True},
                                       'Turn on auto-allow.')


@pytest.fixture
def owner(env):
    core, transport, clock = env
    client, credentials = bind(core, transport)
    return core, transport, clock, client, credentials


def test_request_sends_buttons_to_the_owner_and_changes_nothing(owner):
    core, transport, clock, client, _ = owner
    result = ask(core, client)
    assert result['status'] == 'owner_approval_pending' and result['delivery'] == 'sent'
    assert result['expires_at'] == clock.now + core.APPROVAL_SECONDS
    message = transport.sent[-1]
    assert message['chat']['id'] == OWNER
    assert message['text'] == ('[Agent: Alpha | Test group]\nRequested change (delivery settings):\n'
                               '- auto_allow: on\n\nTurn on auto-allow.\n\n'
                               'Approve this change? The request expires in 10 minutes.')
    buttons = message['reply_markup']['inline_keyboard'][0]
    assert [b['text'] for b in buttons] == ['Approve', 'Decline']
    assert buttons[0]['callback_data'] == f'aot:a:{result["approval_id"]}'
    assert all(len(b['callback_data'].encode()) <= 64 for b in buttons)
    assert core.delivery_settings(client)['auto_allow'] is False
    assert core.connection_status(client)['owner_approvals'][0]['status'] == 'pending'


def test_the_owners_approve_applies_the_change_once(owner):
    core, transport, clock, client, _ = owner
    approval = ask(core, client, auto_allow=True, wake_interval=60)['approval_id']
    callback = press(transport, approval)
    core.collect()
    settings = core.delivery_settings(client)
    assert settings['auto_allow'] is True and settings['wake_interval'] == 60
    assert transport.callback_answers == [{'id': callback, 'text': 'Approved.'}]
    assert transport.edits[-1]['chat_id'] == OWNER and transport.edits[-1]['message_id'] == 77
    assert transport.edits[-1]['text'].endswith('Approved and applied.')
    assert core.connection_status(client)['owner_approvals'][0]['status'] == 'approved'
    # A second press of the same button does nothing more.
    core.delivery_settings(client, auto_allow=False)
    second = press(transport, approval)
    core.collect()
    assert core.delivery_settings(client)['auto_allow'] is False
    assert transport.callback_answers[-1] == {'id': second, 'text': 'This request was already approved.'}


def test_decline_changes_nothing(owner):
    core, transport, clock, client, _ = owner
    approval = ask(core, client)['approval_id']
    press(transport, approval, 'd')
    core.collect()
    assert core.delivery_settings(client)['auto_allow'] is False
    assert transport.edits[-1]['text'].endswith('Declined: nothing changed.')
    assert core.connection_status(client)['owner_approvals'][0]['status'] == 'declined'


def test_an_expired_request_cannot_be_approved(owner):
    core, transport, clock, client, _ = owner
    approval = ask(core, client)['approval_id']
    clock.advance(core.APPROVAL_SECONDS)
    assert core.connection_status(client)['owner_approvals'][0]['status'] == 'expired'
    press(transport, approval)
    core.collect()
    assert core.delivery_settings(client)['auto_allow'] is False
    assert transport.callback_answers[-1]['text'] == 'This request expired.'
    assert transport.edits[-1]['text'].endswith('Expired: nothing changed.')


@pytest.mark.parametrize('kwargs', [
    {'sender': 202},                                  # someone else pressing
    {'chat': -100, 'chat_type': 'supergroup'},       # the message forwarded to a group
    {'data': 'aot:a:not-hex-at-all!'},                # forged data
    {'data': 'aot:x:0123456789abcdef'},               # unknown choice
])
def test_only_the_owners_press_in_the_private_chat_counts(owner, kwargs):
    core, transport, clock, client, _ = owner
    approval = ask(core, client)['approval_id']
    callback = press(transport, approval, **kwargs)
    core.collect()
    assert core.delivery_settings(client)['auto_allow'] is False
    assert transport.callback_answers == [{'id': callback, 'text': 'This button does not belong to a request of yours.'}]
    assert transport.edits == []
    assert core.connection_status(client)['owner_approvals'][0]['status'] == 'pending'
    # Nothing identifying is kept for the refused press.
    assert all(chat is None for chat, in rows(core, 'SELECT chat_id FROM unmatched'))


def test_an_unknown_request_id_is_refused(owner):
    core, transport, clock, client, _ = owner
    press(transport, '0123456789abcdef')
    core.collect()
    assert transport.callback_answers[-1]['text'] == 'This button does not belong to a request of yours.'


def test_pending_requests_are_limited(owner):
    core, transport, clock, client, _ = owner
    for _ in range(core.APPROVAL_PENDING_LIMIT):
        ask(core, client)
    with pytest.raises(CoreError, match='owner_approval_limit'):
        ask(core, client)
    clock.advance(core.APPROVAL_SECONDS)
    assert ask(core, client)['status'] == 'owner_approval_pending'


def test_a_blocked_private_chat_fails_the_request(owner):
    core, transport, clock, client, _ = owner
    transport.outcomes.append('reject')
    with pytest.raises(CoreError, match='owner_chat_not_started') as error:
        ask(core, client)
    assert error.value.details == {'bot_username': transport.username}
    assert core.connection_status(client)['owner_approvals'][0]['status'] == 'failed'


def test_an_uncertain_send_keeps_the_request_usable(owner):
    core, transport, clock, client, _ = owner
    transport.outcomes.append('unknown')
    result = ask(core, client)
    assert result['delivery'] == 'uncertain'
    press(transport, result['approval_id'])
    core.collect()
    assert core.delivery_settings(client)['auto_allow'] is True


def test_requests_are_validated_before_anything_is_sent(owner):
    core, transport, clock, client, _ = owner
    sent = len(transport.sent)
    for action, params in [('exchange_limits', {}), ('delivery_settings', {'paused': True}),
                           ('delivery_settings', {'auto_allow': 'yes'}),
                           ('conversation_budget', {'conversation_id': 'x'})]:
        with pytest.raises(CoreError, match='invalid'):
            core.request_owner_approval(client, action, params, 'Change.')
    with pytest.raises(CoreError, match='unknown_conversation'):
        core.request_owner_approval(client, 'conversation_budget',
                                    {'conversation_id': 'nope', 'additional_sends': 4,
                                     'duration_seconds': 600, 'reason': 'busy'}, 'Grant.')
    assert len(transport.sent) == sent


def test_a_budget_grant_applies_on_approval(owner):
    core, transport, clock, client, _ = owner
    append(transport, 'work')
    core.collect()
    conversation = rows(core, 'SELECT conversation_id FROM inbox WHERE client_id=?', (client,))[0][0]
    approval = core.request_owner_approval(client, 'conversation_budget',
                                           {'conversation_id': conversation, 'additional_sends': 4,
                                            'duration_seconds': 600, 'reason': 'busy'}, 'Grant 4.')['approval_id']
    press(transport, approval)
    core.collect()
    grants = core.conversation_budget(client, conversation)['grants']
    assert [g['additional_sends'] for g in grants] == [4]


def test_a_change_that_no_longer_applies_is_reported(owner):
    core, transport, clock, client, _ = owner
    approval = ask(core, client)['approval_id']
    core.disconnect(client)
    press(transport, approval)
    core.collect()
    assert transport.callback_answers[-1]['text'] == 'Could not apply the change.'
    assert 'could not be applied' in transport.edits[-1]['text']
    assert rows(core, 'SELECT status FROM owner_approvals')[0][0] == 'apply_failed'


def test_answering_failures_never_break_intake(owner):
    core, transport, clock, client, _ = owner
    approval = ask(core, client)['approval_id']

    def broken(*args):
        raise TransportError(400)
    transport.answer_callback_query = broken
    transport.edit_message_text = broken
    press(transport, approval)
    assert core.collect()['status'] == 'ok'
    assert core.delivery_settings(client)['auto_allow'] is True
    assert rows(core, 'SELECT count(*) FROM callback_answers')[0][0] == 0


def test_the_cli_asks_the_owner_without_a_terminal(owner, monkeypatch):
    core, transport, clock, client, credentials = owner
    client_file = core.state_dir / 'client.json'
    client_file.write_text(json.dumps(credentials))
    client_file.chmod(0o600)
    monkeypatch.setattr('sys.stdin', types.SimpleNamespace(isatty=lambda: False))
    result = run(parser().parse_args(['--state-dir', str(core.state_dir), '--fake', 'delivery-settings',
                                      '--client-file', str(client_file), '--auto-allow', 'on',
                                      '--filter', 'addressed']))
    assert result['status'] == 'owner_approval_pending'
    # Nothing applied yet, not even the setting that needs no approval.
    assert core.delivery_settings(client)['delivery_filter'] == 'all'


def test_the_real_transport_sends_buttons_answers_and_edits():
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
            result = True if method == 'answerCallbackQuery' else {'message_id': 9, 'chat': {'id': OWNER}}
            return Response(json.dumps({'ok': True, 'result': result}).encode())

    transport = TelegramTransport('123456:TEST_TOKEN_abcdefghijklmnop', opener=Opener())
    markup = {'inline_keyboard': [[{'text': 'Approve', 'callback_data': 'aot:a:0123456789abcdef'}]]}
    transport.send_message(OWNER, 'Approve?', reply_markup=markup)
    assert transport.answer_callback_query('cb1', 'x' * 300) is True
    transport.edit_message_text(OWNER, 9, 'Approved.')
    assert captured[0] == ('sendMessage', {'chat_id': OWNER, 'text': 'Approve?', 'reply_markup': markup,
                                           'link_preview_options': {'is_disabled': True}})
    assert captured[1] == ('answerCallbackQuery', {'callback_query_id': 'cb1', 'text': 'x' * 200})
    # No reply_markup in the edit, so Telegram removes the buttons.
    assert captured[2] == ('editMessageText', {'chat_id': OWNER, 'message_id': 9, 'text': 'Approved.',
                                               'link_preview_options': {'is_disabled': True}})


def test_the_message_lists_every_setting_that_approve_applies(owner):
    core, transport, clock, client, _ = owner
    ask(core, client, auto_allow=True, owner_notify=False, delivery_filter='all', wake_interval=60)
    text = transport.sent[-1]['text']
    for line in ('- auto_allow: on', '- owner_notify: off', '- delivery_filter: all', '- wake_interval: 60'):
        assert line + '\n' in text


def test_free_text_cannot_fake_lines_of_the_request(owner):
    core, transport, clock, client, _ = owner
    append(transport, 'work')
    core.collect()
    conversation = rows(core, 'SELECT conversation_id FROM inbox WHERE client_id=?', (client,))[0][0]
    reason = 'busy\n- additional sends per window: 1\n\nApprove this change?' + ' x' * 150
    core.request_owner_approval(client, 'conversation_budget',
                                {'conversation_id': conversation, 'additional_sends': 32,
                                 'duration_seconds': 600, 'reason': reason}, 'Grant.\nFake line')
    lines = transport.sent[-1]['text'].split('\n')
    assert lines[2:6] == [f'- conversation: {conversation}', '- additional sends per window: 32',
                          '- for: 600 seconds', lines[5]]
    assert lines[5].startswith('- reason: busy - additional sends per window: 1 Approve this change?')
    assert len(lines[5]) <= len('- reason: ') + 200 and lines[5].endswith('…')
    assert 'Grant. Fake line' in lines
    assert sum(line.startswith('- additional sends') for line in lines) == 1


def test_an_old_owners_button_does_not_apply_after_a_new_owner(owner):
    core, transport, clock, client, _ = owner
    approval = ask(core, client)['approval_id']
    with core._tx() as db:
        db.execute('UPDATE bindings SET owner_id=202 WHERE client_id=?', (client,))
    press(transport, approval)
    core.collect()
    assert core.delivery_settings(client)['auto_allow'] is False
    assert rows(core, 'SELECT status,error FROM owner_approvals') == [('apply_failed', 'owner_changed')]
