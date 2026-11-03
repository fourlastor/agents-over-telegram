"""Owner-only clearing of a suspended binding after Telegram confirms the fix."""
import inspect
import json
import sqlite3

import pytest

from agents_over_telegram import mcp_server
from agents_over_telegram.cli import main, parser, run
from agents_over_telegram.core import Core, CoreError
from agents_over_telegram.credentials import read_private_json, write_private_json
from agents_over_telegram.mcp_server import TOOLS
from agents_over_telegram.transport import FakeTransport, TransportError


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def update(number, text, chat=-100, topic=None, sender=101, **fields):
    message = {'message_id': number, 'chat': {'id': chat, 'type': 'supergroup', 'title': 'Test group'},
               'from': {'id': sender, 'is_bot': False, 'username': 'owner'}, 'text': text}
    if topic is not None:
        message['message_thread_id'] = topic
    message.update(fields)
    return {'update_id': number, 'message': message}


def next_id(transport):
    return max((u['update_id'] for u in transport.updates), default=0) + 1


def append(transport, text, **kwargs):
    transport.updates.append(update(next_id(transport), text, **kwargs))


def membership(transport, status, chat=-100, **fields):
    member = {'user': {'id': transport.bot_id, 'is_bot': True}, 'status': status, **fields}
    transport.updates.append({'update_id': next_id(transport), 'my_chat_member': {
        'chat': {'id': chat, 'type': 'supergroup'}, 'new_chat_member': member}})


def set_member(transport, status, chat=-100, **fields):
    transport.chat_members.setdefault(str(chat), {})[str(transport.bot_id)] = {'status': status, **fields}


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    transport = FakeTransport()
    core = Core(tmp_path / 'state', transport, clock=clock)
    core.register_account(transport.bot_id, transport.username)
    return core, transport


def bind(core, transport, label='Alpha', chat=-100, topic=None):
    client_id = core.create_client(label)['client_id']
    pairing = core.connection_begin(client_id)
    transport.updates.append(update(next_id(transport), pairing['command'], chat, topic))
    core.collect()
    core.confirm_pairing(pairing['pairing_id'])
    return client_id


def suspend_by_removal(core, transport, client_id):
    membership(transport, 'kicked')
    core.collect()
    assert core.connection_status(client_id)['suspended'] == 'bot_removed_or_restricted'


def suspended_column(core, client_id):
    with sqlite3.connect(core.database) as db:
        return db.execute('SELECT suspended FROM bindings WHERE client_id=?', (client_id,)).fetchone()[0]


def test_clear_after_bot_is_added_back(env):
    core, transport = env
    a = bind(core, transport)
    suspend_by_removal(core, transport, a)
    membership(transport, 'member')
    core.collect()
    # A positive membership update alone never lifts the suspension.
    assert core.connection_status(a)['suspended'] == 'bot_removed_or_restricted'
    set_member(transport, 'member')
    result = core.clear_suspension(a)
    assert result['previous_suspension'] == 'bot_removed_or_restricted'
    assert result['suspended'] is None and result['state'] == 'connected'
    assert result['verified']['bot_member_status'] == 'member'
    assert core.connection_status(a)['suspended'] is None


@pytest.mark.parametrize('member', [
    {'status': 'administrator'},
    {'status': 'restricted', 'is_member': True, 'can_send_messages': True},
])
def test_admin_or_restricted_member_who_can_post_is_accepted(env, member):
    core, transport = env
    a = bind(core, transport)
    suspend_by_removal(core, transport, a)
    set_member(transport, **member)
    assert core.clear_suspension(a)['suspended'] is None


@pytest.mark.parametrize('member,code', [
    (None, 'bot_not_in_chat'),
    ({'status': 'left'}, 'bot_cannot_post'),
    ({'status': 'kicked'}, 'bot_cannot_post'),
    ({'status': 'restricted', 'is_member': True, 'can_send_messages': False}, 'bot_cannot_post'),
    ({'status': 'restricted', 'is_member': False, 'can_send_messages': True}, 'bot_cannot_post'),
    ({'status': 'restricted'}, 'bot_cannot_post'),
])
def test_refuses_while_bot_is_still_removed_or_restricted(env, member, code):
    core, transport = env
    a = bind(core, transport)
    suspend_by_removal(core, transport, a)
    if member is not None:
        set_member(transport, **member)
    with pytest.raises(CoreError, match=code):
        core.clear_suspension(a)
    assert suspended_column(core, a) == 'bot_removed_or_restricted'


def test_network_failure_leaves_flag_and_reports_check_failure(env, monkeypatch):
    core, transport = env
    a = bind(core, transport)
    suspend_by_removal(core, transport, a)

    def broken(chat_id, user_id):
        raise TransportError('transport_failure')
    monkeypatch.setattr(transport, 'get_chat_member', broken)
    with pytest.raises(CoreError, match='telegram_check_failed'):
        core.clear_suspension(a)
    assert suspended_column(core, a) == 'bot_removed_or_restricted'


def test_refuses_migration_without_asking_telegram(env, monkeypatch):
    core, transport = env
    a = bind(core, transport)
    append(transport, 'service', migrate_to_chat_id=-999)
    core.collect()
    set_member(transport, 'member')
    monkeypatch.setattr(transport, 'get_chat_member', lambda *args: pytest.fail('migration must not be checked'))
    with pytest.raises(CoreError, match='group_migrated_pair_again'):
        core.clear_suspension(a)
    assert suspended_column(core, a) == 'group_migrated:-999'
    status = core.connection_status(a)
    assert 'pair again' in status['next_action']['message']


def test_topic_change_needs_owner_confirmation_and_bot_membership(env):
    core, transport = env
    a = bind(core, transport, topic=7)
    append(transport, 'service', topic=7, forum_topic_closed={})
    core.collect()
    assert core.connection_status(a)['suspended'] == 'topic_changed'
    assert '--topic-open' in core.connection_status(a)['next_action']['message']
    with pytest.raises(CoreError, match='topic_open_confirmation_required'):
        core.clear_suspension(a)
    with pytest.raises(CoreError, match='bot_not_in_chat'):
        core.clear_suspension(a, topic_open_confirmed=True)
    assert suspended_column(core, a) == 'topic_changed'
    set_member(transport, 'member')
    result = core.clear_suspension(a, topic_open_confirmed=True)
    assert result['previous_suspension'] == 'topic_changed'
    assert result['verified']['topic_open'] == 'owner_confirmed'


def test_not_suspended_is_refused(env):
    core, transport = env
    a = bind(core, transport)
    with pytest.raises(CoreError, match='not_suspended'):
        core.clear_suspension(a)


def test_newer_suspension_during_check_is_kept(env, monkeypatch):
    core, transport = env
    a = bind(core, transport)
    suspend_by_removal(core, transport, a)

    def migrate_during_check(chat_id, user_id):
        with sqlite3.connect(core.database) as db:
            db.execute("UPDATE bindings SET suspended='group_migrated:-5' WHERE client_id=?", (a,))
        return {'status': 'member'}
    monkeypatch.setattr(transport, 'get_chat_member', migrate_during_check)
    with pytest.raises(CoreError, match='suspension_changed'):
        core.clear_suspension(a)
    assert suspended_column(core, a) == 'group_migrated:-5'


def test_queued_messages_are_delivered_after_clear(env):
    core, transport = env
    a = bind(core, transport)
    b = bind(core, transport, 'Beta', chat=-200)
    suspend_by_removal(core, transport, a)
    append(transport, 'held one')
    append(transport, 'held two')
    core.collect()
    assert core.poll(a)['deliveries'] == []
    with pytest.raises(CoreError, match='connection_suspended'):
        core.send(a, 'blocked while suspended', 'k1')
    set_member(transport, 'member')
    result = core.clear_suspension(a)
    assert result['pending_messages'] == 2
    page = core.poll(a)
    assert [d['text'] for d in page['deliveries']] == ['held one', 'held two']
    core.ack(a, [d['delivery_id'] for d in page['deliveries']], page['lease_token'])
    assert core.send(a, 'back again', 'k2')['status'] == 'sent'
    assert core.connection_status(b)['suspended'] is None


def test_clear_keeps_an_owner_pause(env):
    core, transport = env
    a = bind(core, transport)
    core.pause(a)
    suspend_by_removal(core, transport, a)
    set_member(transport, 'member')
    result = core.clear_suspension(a)
    assert result['paused'] is True and result['state'] == 'paused'


def test_unsuspend_is_not_an_mcp_tool():
    names = {tool.name for tool in TOOLS}
    assert not any('suspend' in name for name in names)
    assert 'clear_suspension' not in inspect.getsource(mcp_server)


def cli(state, *args):
    return run(parser().parse_args(['--state-dir', str(state), '--fake', *args]))


def cli_suspended_setup(tmp_path):
    state = tmp_path / 'state'
    cli(state, 'account-register')
    client_file = state / 'clients' / 'alpha.json'
    cli(state, 'client-create', '--label', 'Alpha', '--client-file', str(client_file))
    pairing = cli(state, 'pair-begin', '--client-file', str(client_file))
    fake_path = state / 'fake-transport.json'
    fake = read_private_json(fake_path)
    bot_id = fake['bot_id']
    fake['updates'] = [update(1, pairing['command'])]
    write_private_json(fake_path, fake, overwrite=True)
    cli(state, 'collect')
    cli(state, 'pair-confirm', '--pairing-id', pairing['pairing_id'], '--confirm')
    fake = read_private_json(fake_path)
    fake['updates'] += [
        {'update_id': 2, 'my_chat_member': {'chat': {'id': -100}, 'new_chat_member': {
            'user': {'id': bot_id}, 'status': 'left'}}},
        update(3, 'held while removed'),
    ]
    write_private_json(fake_path, fake, overwrite=True)
    cli(state, 'collect')
    return state, client_file, fake_path, bot_id


def test_cli_unsuspend_refuses_then_clears_and_prints_json(tmp_path, monkeypatch, capsys):
    state, client_file, fake_path, bot_id = cli_suspended_setup(tmp_path)
    assert cli(state, 'status', '--client-file', str(client_file))['suspended'] == 'bot_removed_or_restricted'
    args = ['aot', '--state-dir', str(state), '--fake', 'unsuspend', '--client-file', str(client_file)]
    monkeypatch.setattr('sys.argv', args)
    assert main() == 1
    assert json.loads(capsys.readouterr().err) == {'ok': False, 'error': 'bot_not_in_chat'}
    fake = read_private_json(fake_path)
    fake['chat_members'] = {'-100': {str(bot_id): {'status': 'member', 'user': {'id': bot_id, 'first_name': 'private'}}}}
    write_private_json(fake_path, fake, overwrite=True)
    assert main() == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed['previous_suspension'] == 'bot_removed_or_restricted'
    assert printed['suspended'] is None
    assert printed['pending_messages'] == 1
    assert 'private' not in json.dumps(printed)
    assert cli(state, 'status', '--client-file', str(client_file))['state'] == 'connected'
