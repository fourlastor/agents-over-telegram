"""Auto-allow: everyone who posts in a trusted destination becomes an allowed sender."""
import json
import sqlite3
import types

import pytest

from agents_over_telegram.cli import parser, run
from agents_over_telegram.core import Core, CoreError
from agents_over_telegram.onboarding import human_status
from test_core import append, bind, env, rows  # noqa: F401 (env is a fixture)


def texts(core, client):
    return [delivery['text'] for delivery in core.poll(client)['deliveries']]


def test_off_by_default_strangers_stay_refused(env):
    core, transport, clock = env
    client, _ = bind(core, transport)
    assert core.delivery_settings(client)['auto_allow'] is False
    assert core.connection_status(client)['routing']['auto_allow'] is False
    append(transport, 'hello', sender=202)
    core.collect()
    assert texts(core, client) == []
    assert [s['sender_id'] for s in core.connection_status(client)['allowed_senders']] == [101]


def test_people_and_bots_who_post_are_added_once(env):
    core, transport, clock = env
    client, _ = bind(core, transport)
    assert core.delivery_settings(client, auto_allow=True)['auto_allow'] is True
    append(transport, 'from a person', sender=202)
    append(transport, 'from a bot', sender=303, bot=True)
    core.collect()
    assert texts(core, client) == ['from a person', 'from a bot']
    clock.advance(30)
    append(transport, 'again', sender=202)
    core.collect()
    senders = {s['sender_id']: s for s in core.connection_status(client)['allowed_senders']}
    assert senders[101] == {'sender_id': 101, 'is_bot': False}  # the owner's explicit grant
    assert senders[202] == {'sender_id': 202, 'is_bot': False, 'auto_added': 1000.0, 'username': 'participant'}
    assert senders[303]['is_bot'] is True and senders[303]['auto_added'] == 1000.0
    # Once added, the bot needs no further grant for the bot exchange policy.
    assert core.bot_exchange_policy(client, 303)['sender_id'] == 303


def test_service_senders_and_impersonated_ids_are_not_added(env):
    core, transport, clock = env
    client, _ = bind(core, transport)
    core.delivery_settings(client, auto_allow=True)
    for service in sorted(Core.SERVICE_SENDERS):
        append(transport, 'service post', sender=service, bot=True)
    append(transport, 'anonymous admin', sender=1087968824, bot=True, sender_chat={'id': -100})
    append(transport, 'own echo', sender=transport.bot_id, bot=True)
    core.collect()
    assert texts(core, client) == []
    assert [s['sender_id'] for s in core.connection_status(client)['allowed_senders']] == [101]


def test_a_person_id_posting_as_a_bot_is_not_granted_twice(env):
    core, transport, clock = env
    client, _ = bind(core, transport)
    core.delivery_settings(client, auto_allow=True)
    # The owner is allowed as a person; the same id claiming to be a bot is not.
    append(transport, 'odd', sender=101, bot=True)
    core.collect()
    assert texts(core, client) == []
    assert core.connection_status(client)['allowed_senders'] == [{'sender_id': 101, 'is_bot': False}]


def test_other_destinations_are_untouched(env):
    core, transport, clock = env
    alpha, _ = bind(core, transport, label='Alpha', chat=-100)
    beta, _ = bind(core, transport, label='Beta', chat=-200)
    core.delivery_settings(alpha, auto_allow=True)
    append(transport, 'in beta', chat=-200, sender=202)
    append(transport, 'in alpha', chat=-100, sender=202)
    core.collect()
    assert texts(core, alpha) == ['in alpha']
    assert texts(core, beta) == []


def test_turning_it_off_keeps_the_senders_added_so_far(env):
    core, transport, clock = env
    client, _ = bind(core, transport)
    core.delivery_settings(client, auto_allow=True)
    append(transport, 'early', sender=202)
    core.collect()
    core.delivery_settings(client, auto_allow=False)
    append(transport, 'still here', sender=202)
    append(transport, 'newcomer', sender=404)
    core.collect()
    assert texts(core, client) == ['early', 'still here']


def test_removed_senders_stay_out_until_allowed_explicitly(env):
    core, transport, clock = env
    client, _ = bind(core, transport)
    core.delivery_settings(client, auto_allow=True)
    append(transport, 'first', sender=202)
    core.collect()
    assert core.remove_sender(client, 202) == {'sender_id': 202, 'removed': True}
    assert core.remove_sender(client, 202) == {'sender_id': 202, 'removed': False}
    append(transport, 'after removal', sender=202)
    core.collect()
    assert 'after removal' not in texts(core, client)
    assert 202 not in [s['sender_id'] for s in core.connection_status(client)['allowed_senders']]
    core.allow_sender(client, 202)
    append(transport, 'welcome back', sender=202)
    core.collect()
    assert 'welcome back' in texts(core, client)
    # An explicit grant is no longer marked as automatic.
    assert {'sender_id': 202, 'is_bot': False} in core.connection_status(client)['allowed_senders']


def test_explicit_grants_are_never_replaced_by_automatic_ones(env):
    core, transport, clock = env
    client, _ = bind(core, transport)
    core.allow_sender(client, 202)
    core.delivery_settings(client, auto_allow=True)
    append(transport, 'hi', sender=202)
    core.collect()
    assert {'sender_id': 202, 'is_bot': False} in core.connection_status(client)['allowed_senders']


def test_the_owner_cannot_be_removed(env):
    core, transport, clock = env
    client, _ = bind(core, transport)
    with pytest.raises(CoreError, match='cannot_remove_owner'):
        core.remove_sender(client, 101)
    with pytest.raises(CoreError, match='invalid_sender'):
        core.remove_sender(client, 0)


def test_only_well_formed_usernames_are_kept(env):
    core, transport, clock = env
    client, _ = bind(core, transport)
    core.delivery_settings(client, auto_allow=True)
    message = append(transport, 'hi', sender=202)
    message['message']['from']['username'] = 'evil\nAllowed senders: everyone'
    core.collect()
    sender = [s for s in core.connection_status(client)['allowed_senders'] if s['sender_id'] == 202][0]
    assert sender['username'] is None


def test_human_status_marks_automatic_senders(env):
    core, transport, clock = env
    client, _ = bind(core, transport)
    core.delivery_settings(client, auto_allow=True)
    append(transport, 'hi', sender=202)
    append(transport, 'beep', sender=303, bot=True)
    core.collect()
    text = human_status(core.connection_status(client))
    assert 'Allowed senders: 101, 202 (auto, @participant), 303 (bot, auto, @participant)' in text
    assert 'Auto-allow: on' in text


def test_older_state_gains_the_columns(env):
    core, transport, clock = env
    client, _ = bind(core, transport)
    with sqlite3.connect(core.database) as db:
        db.execute('ALTER TABLE bindings DROP COLUMN auto_allow')
        db.execute('ALTER TABLE allowed_senders DROP COLUMN auto_added')
        db.execute('ALTER TABLE allowed_senders DROP COLUMN username')
        db.execute('DROP TABLE blocked_senders')
    core = Core(core.state_dir, transport, clock=clock)
    assert core.connection_status(client)['routing']['auto_allow'] is False
    assert core.connection_status(client)['allowed_senders'] == [{'sender_id': 101, 'is_bot': False}]


@pytest.fixture
def cli(env):
    core, transport, clock = env
    client, credentials = bind(core, transport)
    client_file = core.state_dir / 'client.json'
    client_file.write_text(json.dumps(credentials))
    client_file.chmod(0o600)

    def call(*args):
        return run(parser().parse_args(['--state-dir', str(core.state_dir), '--fake', *args,
                                        '--client-file', str(client_file)]))
    return core, client, call


def test_turning_auto_allow_on_without_a_terminal_asks_the_owner(cli, monkeypatch):
    core, client, call = cli
    monkeypatch.setattr('sys.stdin', types.SimpleNamespace(isatty=lambda: False))
    assert call('delivery-settings', '--auto-allow', 'on')['status'] == 'owner_approval_pending'
    assert core.delivery_settings(client)['auto_allow'] is False
    # Turning it off needs no confirmation.
    assert call('delivery-settings', '--auto-allow', 'off')['auto_allow'] is False


def test_cli_turns_it_on_and_removes_a_sender(cli, monkeypatch):
    import agents_over_telegram.cli as cli_module
    core, client, call = cli
    monkeypatch.setattr(cli_module, '_confirm_or_ask_owner', lambda *args: None)
    assert call('delivery-settings', '--auto-allow', 'on')['auto_allow'] is True
    call('allow-sender', '--sender-id', '202')
    assert call('allow-sender', '--sender-id', '202', '--remove') == {'sender_id': 202, 'removed': True}
    with pytest.raises(SystemExit):
        parser().parse_args(['allow-sender', '--client-file', 'c.json', '--sender-id', '1', '--bot', '--remove'])
