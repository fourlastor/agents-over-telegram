"""The owner's settings panel widget: sending, owner-only changes, answers and limits."""
import json
import zlib

import pytest

from agents_over_telegram import settings_panel
from agents_over_telegram.cli import parser, run
from agents_over_telegram.core import CoreError
from agents_over_telegram.transport import TransportError
from test_core import bind, env, rows  # noqa: F401 (env is a fixture)

OWNER = 101


@pytest.fixture
def panel(env):
    core, transport, clock = env
    client, credentials = bind(core, transport)
    sent = core.send_settings_panel(client)
    return core, transport, clock, client, credentials, sent['message_id']


def query(transport, message_id, data, sender=OWNER, chat=None, chat_type='private', **extra):
    number = max((u['update_id'] for u in transport.updates), default=0) + 1
    body = {'id': f'q{number}', 'from': {'id': sender, 'is_bot': False},
            'chat': {'id': sender if chat is None else chat, 'type': chat_type},
            'message_id': message_id, 'data': data, **extra}
    transport.updates.append({'update_id': number, 'widget_query': body})
    return f'q{number}'


def k(client):
    return settings_panel.key(client)


def answered(transport, query_id):
    return json.loads(next(a['data'] for a in transport.widget_answers if a['id'] == query_id))


def test_the_panel_goes_to_the_owner_with_the_current_settings(panel):
    core, transport, clock, client, _, message_id = panel
    widget = transport.widgets[-1]
    assert widget['chat']['id'] == OWNER and widget['widget']['allow_queries'] is True
    state = json.loads(widget['widget']['chat_data'])
    assert state['c'] == [['Alpha', 'Test group', k(client)]] and state['k'] == k(client)
    assert len(state['v']) == len(settings_panel.SETTINGS)
    names = [name for name, *_ in settings_panel.SETTINGS]
    assert state['v'][names.index('accept_media')] == 1  # new connections accept media
    assert state['v'][names.index('auto_allow')] == 0
    assert rows(core, 'SELECT owner_id,message_id FROM settings_panels') == [(OWNER, message_id)]


def test_the_widget_fits_telegrams_limits():
    html = settings_panel.html().encode()
    assert len(html) <= 32768 and len(zlib.compress(html, 9)) <= 4096
    many = [{'client_id': f'agent_{i:024x}', 'label': 'L' * 64, 'title': 'T' * 128} for i in range(20)]
    worst = settings_panel.state(many, many[0]['client_id'], {'wake_interval': 86400})
    assert len(worst.encode()) <= 1024


def test_a_tap_changes_the_setting_and_refreshes_the_panel(panel):
    core, transport, clock, client, _, message_id = panel
    q = query(transport, message_id, f's:{k(client)}:0:1')  # Deliver: Addressed
    core.collect()
    assert core.delivery_settings(client)['delivery_filter'] == 'addressed'
    assert answered(transport, q)['s']['v'][0] == 1
    assert transport.widget_edits[-1]['chat_id'] == OWNER and transport.widget_edits[-1]['message_id'] == message_id
    assert json.loads(transport.widget_edits[-1]['chat_data'])['v'][0] == 1
    assert rows(core, 'SELECT count(*) FROM widget_answers')[0][0] == 0


@pytest.mark.parametrize('setting', sorted(settings_panel.WARNINGS))
def test_settings_that_warn_need_a_second_tap(panel, setting):
    core, transport, clock, client, _, message_id = panel
    index = [name for name, *_ in settings_panel.SETTINGS].index(setting)
    first = query(transport, message_id, f's:{k(client)}:{index}:1')
    core.collect()
    assert answered(transport, first) == {'w': settings_panel.WARNINGS[setting]}
    assert core.delivery_settings(client)[setting] is False
    assert transport.widget_edits == []
    query(transport, message_id, f'c:{k(client)}:{index}:1')
    core.collect()
    assert core.delivery_settings(client)[setting] is True
    # Turning it off needs no second tap.
    query(transport, message_id, f's:{k(client)}:{index}:0')
    core.collect()
    assert core.delivery_settings(client)[setting] is False


def test_a_refresh_answers_without_editing(panel):
    core, transport, clock, client, _, message_id = panel
    core.delivery_settings(client, wake_interval=45)
    q = query(transport, message_id, f'v:{k(client)}')
    core.collect()
    state = answered(transport, q)['s']
    assert state['wi'] == 45 and state['v'][4] == -1  # not one of the presets
    assert transport.widget_edits == []


@pytest.mark.parametrize('kwargs', [
    {'sender': 202},                                   # someone else
    {'chat': -100, 'chat_type': 'supergroup'},        # not the private chat
    {'chat': 999},                                     # chat id not the presser's
])
def test_only_the_owner_in_their_private_chat_changes_anything(panel, kwargs):
    core, transport, clock, client, _, message_id = panel
    q = query(transport, message_id, f's:{k(client)}:0:1', **kwargs)
    core.collect()
    assert core.delivery_settings(client)['delivery_filter'] == 'all'
    assert answered(transport, q) == {'e': 'not allowed'}
    assert transport.widget_edits == []
    assert all(chat is None for chat, in rows(core, 'SELECT chat_id FROM unmatched'))


def test_only_the_latest_panel_counts(panel):
    core, transport, clock, client, _, old = panel
    new = core.send_settings_panel(client)['message_id']
    stale = query(transport, old, f's:{k(client)}:0:1')
    core.collect()
    assert answered(transport, stale) == {'e': 'not allowed'}
    query(transport, new, f's:{k(client)}:0:1')
    core.collect()
    assert core.delivery_settings(client)['delivery_filter'] == 'addressed'


def test_a_new_owner_or_a_disconnect_ends_the_panel(panel):
    core, transport, clock, client, _, message_id = panel
    with core._tx() as db:
        db.execute('UPDATE bindings SET owner_id=202 WHERE client_id=?', (client,))
    q = query(transport, message_id, f's:{k(client)}:0:1')
    core.collect()
    assert answered(transport, q) == {'e': 'not allowed'}
    with core._tx() as db:
        db.execute('UPDATE bindings SET owner_id=? WHERE client_id=?', (OWNER, client))
    core.disconnect(client)
    q = query(transport, message_id, f's:{k(client)}:0:1')
    core.collect()
    assert answered(transport, q) == {'e': 'not allowed'}


@pytest.mark.parametrize('data', ['s:K:10:0', 's:K:0:2', 's:K:4:9', 'x:K:0:1', 's:K:0', 's:K:0:1:2', 's:K:-1:0',
                                  'S:K:0:1', 's:K:00001:1', '', 'v:K ', 's:K:٣:1', 's:0:1', 'r'])
def test_unknown_requests_change_nothing(panel, data):
    core, transport, clock, client, _, message_id = panel
    before = core.delivery_settings(client)
    q = query(transport, message_id, data.replace('K', k(client)))
    core.collect()
    assert core.delivery_settings(client) == before
    assert answered(transport, q) == {'e': 'unknown request'}


def test_a_malformed_query_is_dropped(panel):
    core, transport, clock, client, _, message_id = panel
    transport.updates.append({'update_id': 99, 'widget_query': {'from': {'id': OWNER}}})
    assert core.collect()['status'] == 'ok'
    assert transport.widget_answers == []


def test_answering_failures_never_break_intake(panel):
    core, transport, clock, client, _, message_id = panel

    def broken(*args):
        raise TransportError(400)
    transport.answer_widget_query = broken
    transport.edit_message_widget_data = broken
    query(transport, message_id, f's:{k(client)}:0:1')
    assert core.collect()['status'] == 'ok'
    assert core.delivery_settings(client)['delivery_filter'] == 'addressed'
    assert rows(core, 'SELECT count(*) FROM widget_answers')[0][0] == 0


def test_a_blocked_private_chat_fails_the_send(env):
    core, transport, clock = env
    client, _ = bind(core, transport)
    transport.outcomes.append('reject')
    with pytest.raises(CoreError, match='owner_chat_not_started'):
        core.send_settings_panel(client)
    assert rows(core, 'SELECT count(*) FROM settings_panels')[0][0] == 0


def test_the_cli_sends_the_panel(env):
    core, transport, clock = env
    client, credentials = bind(core, transport)
    client_file = core.state_dir / 'client.json'
    client_file.write_text(json.dumps(credentials))
    client_file.chmod(0o600)
    result = run(parser().parse_args(['--state-dir', str(core.state_dir), '--fake', 'settings-panel',
                                      '--client-file', str(client_file)]))
    assert result['status'] == 'sent'


def test_the_real_transport_sends_answers_and_edits_widgets():
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
            result = {'message_id': 9, 'chat': {'id': OWNER}} if method == 'sendWidget' else True
            return Response(json.dumps({'ok': True, 'result': result}).encode())

    transport = TelegramTransport('123456:TEST_TOKEN_abcdefghijklmnop', opener=Opener())
    assert transport.send_widget(OWNER, '<p>x</p>', chat_data='{}', allow_queries=True)['message_id'] == 9
    transport.answer_widget_query('q1', '{"s":1}')
    transport.edit_message_widget_data(OWNER, 9, '{"v":[]}')
    assert captured == [
        ('sendWidget', {'chat_id': OWNER, 'html': '<p>x</p>', 'allow_queries': True, 'chat_data': '{}'}),
        ('answerWidgetQuery', {'widget_query_id': 'q1', 'data': '{"s":1}'}),
        ('editMessageWidgetData', {'chat_id': OWNER, 'message_id': 9, 'chat_data': '{"v":[]}'}),
    ]


def test_one_panel_covers_all_the_owners_connections(env):
    core, transport, clock = env
    alpha, _ = bind(core, transport, label='Alpha', chat=-100)
    beta, _ = bind(core, transport, label='Beta', chat=-200)
    other, _ = bind(core, transport, label='Gamma', chat=-300, sender=303)
    message_id = core.send_settings_panel(beta)['message_id']
    state = json.loads(transport.widgets[-1]['widget']['chat_data'])
    assert [c[0] for c in state['c']] == ['Alpha', 'Beta'] and state['k'] == k(beta)
    # Picking Alpha shows its values without changing anything.
    q = query(transport, message_id, f'v:{k(alpha)}')
    core.collect()
    assert answered(transport, q)['s']['k'] == k(alpha) and transport.widget_edits == []
    query(transport, message_id, f's:{k(alpha)}:0:1')
    core.collect()
    assert core.delivery_settings(alpha)['delivery_filter'] == 'addressed'
    assert core.delivery_settings(beta)['delivery_filter'] == 'all'
    assert json.loads(transport.widget_edits[-1]['chat_data'])['k'] == k(alpha)
    # Another owner's connection can't be reached from this panel.
    q = query(transport, message_id, f's:{k(other)}:0:1')
    core.collect()
    assert answered(transport, q) == {'e': 'not allowed'}
    assert core.delivery_settings(other)['delivery_filter'] == 'all'


def private_message(transport, text, sender=OWNER):
    number = max((u['update_id'] for u in transport.updates), default=0) + 1
    transport.updates.append({'update_id': number, 'message': {
        'message_id': number, 'chat': {'id': sender, 'type': 'private'},
        'from': {'id': sender, 'is_bot': False}, 'text': text}})


def test_settings_in_the_private_chat_sends_the_panel(env):
    core, transport, clock = env
    client, _ = bind(core, transport)
    private_message(transport, '/settings')
    core.collect()
    assert len(transport.widgets) == 1 and transport.widgets[0]['chat']['id'] == OWNER
    message_id = transport.widgets[0]['message_id']
    query(transport, message_id, f's:{k(client)}:0:1')
    core.collect()
    assert core.delivery_settings(client)['delivery_filter'] == 'addressed'
    # Asked again at once: no second panel; later: a new one.
    private_message(transport, f'/settings@{transport.username}')
    core.collect()
    assert len(transport.widgets) == 1
    clock.advance(core.PANEL_REQUEST_INTERVAL)
    private_message(transport, '/settings')
    core.collect()
    assert len(transport.widgets) == 2
    # /settings is not answered as a /start would be.
    assert not any('Ready.' in str(m.get('text')) for m in transport.sent)


def test_settings_from_someone_without_agents_sends_nothing(env):
    core, transport, clock = env
    bind(core, transport)
    private_message(transport, '/settings', sender=202)
    core.collect()
    assert transport.widgets == []
