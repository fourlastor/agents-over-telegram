"""Deterministic offline proof of reaction send/receive semantics (issue #23)."""
import json
import sqlite3

import pytest

from agents_over_telegram.core import Core, CoreError
from agents_over_telegram.transport import FakeTransport, TransportError


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds=2):
        self.now += seconds


def message(number, text, chat=-100, topic=None, sender=101, bot=False, **fields):
    payload = {'message_id': number, 'chat': {'id': chat, 'type': 'supergroup', 'title': 'Test group'},
               'from': {'id': sender, 'is_bot': bot, 'username': 'participant'}, 'text': text}
    if topic is not None:
        payload['message_thread_id'] = topic
        payload['chat']['is_forum'] = True
        payload['is_topic_message'] = True
    payload.update(fields)
    return {'update_id': number, 'message': payload}


def reaction(number, message_id, chat=-100, sender=101, bot=False, old=(), new=(), **fields):
    payload = {'chat': {'id': chat, 'type': 'supergroup', 'title': 'Test group'},
               'message_id': message_id, 'date': 1000,
               'user': {'id': sender, 'is_bot': bot, 'username': 'participant'},
               'old_reaction': list(old), 'new_reaction': list(new)}
    payload.update(fields)
    return {'update_id': number, 'message_reaction': payload}


def emoji(items):
    return [{'type': 'emoji', 'emoji': item} for item in items]


def next_number(transport):
    return max((u['update_id'] for u in transport.updates), default=0) + 1


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    transport = FakeTransport()
    core = Core(tmp_path / 'state', transport, clock=clock)
    core.register_account(transport.bot_id, transport.username)
    return core, transport, clock


def bind(core, transport, label='Alpha', chat=-100, topic=None, sender=101):
    credentials = core.create_client(label)
    pairing = core.connection_begin(credentials['client_id'])
    transport.updates.append(message(next_number(transport), pairing['command'], chat, topic, sender))
    core.collect()
    core.confirm_pairing(pairing['pairing_id'])
    return credentials['client_id']


def collect_one(core, transport, update_dict):
    transport.updates.append(update_dict)
    assert core.collect()['status'] == 'ok'


def rows(core, sql, args=()):
    with sqlite3.connect(core.database) as db:
        return db.execute(sql, args).fetchall()


def unassigned(core):
    return [row[0] for row in rows(core, 'SELECT reason FROM unmatched')]


def send_published(core, client, text='hello'):
    """Send one message and return its published Telegram message id."""
    result = core.send(client, text, 'send-' + text + '-' + str(rows(core, 'SELECT count(*) FROM outbox')[0][0]))
    assert result['status'] == 'sent'
    return result['message_id']


# ---------------------------------------------------------------- message_react


def test_react_validates_emoji_against_official_allowlist(env):
    core, transport, clock = env
    client = bind(core, transport)
    message_id = send_published(core, client)
    with pytest.raises(CoreError, match='invalid_reaction_emoji'):
        core.react(client, message_id, 'x', 'k1')
    with pytest.raises(CoreError, match='invalid_reaction_emoji'):
        core.react(client, message_id, '❌', 'k2')
    with pytest.raises(CoreError, match='invalid_reaction_emoji'):
        core.react(client, message_id, 7, 'k3')
    with pytest.raises(CoreError, match='invalid_reaction_emoji'):
        core.react(client, message_id, '\ufe0f', 'k4')
    with pytest.raises(CoreError, match='invalid_reaction'):
        core.react(client, 0, '👍', 'k5')
    # The documented bare forms and their variation-selector forms both work,
    # and are stored normalized for Telegram's exact comparison.
    assert core.react(client, message_id, '❤️', 'k6')['emoji'] == '❤'
    clock.advance()
    assert core.react(client, message_id, '🤷‍♂️', 'k7')['emoji'] == '🤷‍♂'
    clock.advance()
    assert core.react(client, message_id, '👀', 'k8')['status'] == 'sent'
    clock.advance()
    removal = core.react(client, message_id, '', 'k9')
    assert (removal['emoji'], removal['status']) == ('', 'sent')
    assert transport.reactions == [
        {'chat_id': -100, 'message_id': message_id, 'emoji': '❤'},
        {'chat_id': -100, 'message_id': message_id, 'emoji': '🤷‍♂'},
        {'chat_id': -100, 'message_id': message_id, 'emoji': '👀'},
        {'chat_id': -100, 'message_id': message_id, 'emoji': ''},
    ]


def test_react_only_targets_messages_in_the_bound_destination(env):
    core, transport, clock = env
    alpha = bind(core, transport, 'Alpha')
    beta = bind(core, transport, 'Beta', chat=-200)
    other_chat_message = send_published(core, beta, 'elsewhere')
    with pytest.raises(CoreError, match='unknown_reaction_target'):
        core.react(alpha, other_chat_message, '👍', 'k1')
    with pytest.raises(CoreError, match='unknown_reaction_target'):
        core.react(alpha, 999999, '👍', 'k2')
    # A captured incoming message and the agent's own published message both work.
    collect_one(core, transport, message(next_number(transport), 'hi', sender=101))
    incoming = core.poll(alpha)['deliveries'][0]['message_id']
    clock.advance()
    assert core.react(alpha, incoming, '👍', 'k3')['status'] == 'sent'
    clock.advance()
    own = send_published(core, alpha, 'own')
    assert core.react(alpha, own, '🔥', 'k4')['status'] == 'sent'


def test_react_idempotency_reuses_intent_and_rejects_conflicts(env):
    core, transport, clock = env
    client = bind(core, transport)
    message_id = send_published(core, client)
    first = core.react(client, message_id, '👍', 'intent-1')
    second = core.react(client, message_id, '👍', 'intent-1')
    assert second['reaction_id'] == first['reaction_id']
    assert len(transport.reactions) == 1
    with pytest.raises(CoreError, match='idempotency_conflict'):
        core.react(client, message_id, '👎', 'intent-1')


def test_reactions_have_a_separate_rate_limit(env):
    core, transport, clock = env
    client = bind(core, transport)
    message_id = send_published(core, client)
    with sqlite3.connect(core.database) as db:
        for n in range(core.REACTION_SEND_LIMIT):
            db.execute('INSERT INTO reaction_outbox VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       ('react_seed%d' % n, client, -100, 0, message_id, '👍', 'seed%d' % n,
                        'x' * 64, 'sent', None, clock() - n, clock() - n, clock() - n))
    with pytest.raises(CoreError) as error:
        core.react(client, message_id, '👀', 'over')
    blocked = error.value
    assert blocked.code == 'reaction_limit'
    assert blocked.details['limit'] == core.REACTION_SEND_LIMIT
    assert blocked.details['sent_in_window'] == core.REACTION_SEND_LIMIT
    assert blocked.details['window_seconds'] == core.REACTION_WINDOW
    assert blocked.retry_at is not None
    # Message sends are untouched: the reaction limit blocks neither sending nor
    # the message budgets, and message usage never blocked the reaction before.
    clock.advance()
    assert core.send(client, 'still fine', 'msg-after-limit')['status'] == 'sent'
    with sqlite3.connect(core.database) as db:
        db.execute('UPDATE reaction_outbox SET created=created-? WHERE client_id=?', (core.REACTION_WINDOW + 10, client))
        db.execute("UPDATE outbox SET created=created-? WHERE client_id=?", (core.CLIENT_WINDOW + 10, client))
    clock.advance()
    # A full message window does not stop reactions either.
    with sqlite3.connect(core.database) as db:
        for n in range(core.REACTION_SEND_LIMIT):
            db.execute('INSERT INTO reaction_outbox VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       ('react_old%d' % n, client, -100, 0, message_id, '👍', 'old%d' % n,
                        'x' * 64, 'sent', None, clock() - core.REACTION_WINDOW - n,
                        clock() - core.REACTION_WINDOW - n, clock() - core.REACTION_WINDOW - n))
    assert core.react(client, message_id, '👀', 'fresh-window')['status'] == 'sent'


def test_react_flush_outcomes_reject_rate_limit_and_unknown(env):
    core, transport, clock = env
    client = bind(core, transport)
    message_id = send_published(core, client)
    transport.outcomes.append('reject')
    rejected = core.react(client, message_id, '👍', 'reject-1')
    assert (rejected['status'], rejected['error']) == ('rejected', 'telegram_rejected')
    with pytest.raises(CoreError, match='unknown_delivery'):
        core.delivery_status(client, 'react_missing')
    clock.advance()
    transport.outcomes.append('rate_limit')
    deferred = core.react(client, message_id, '👍', 'defer-1')
    assert (deferred['status'], deferred['error']) == ('queued', 'rate_limited')
    assert len(transport.reactions) == 0  # neither the rejected nor the rate-limited attempt reached Telegram
    clock.advance(deferred['retry_at'] - clock())
    flushed = core.flush(client_id=client)
    assert flushed['reactions'] == 1
    assert core.delivery_status(client, deferred['reaction_id'])['status'] == 'sent'
    clock.advance()
    transport.outcomes.append('unknown')
    unknown = core.react(client, message_id, '👎', 'unknown-1')
    assert (unknown['status'], unknown['error']) == ('unknown', 'uncertain_send')
    resolved = core.resolve_unknown(unknown['reaction_id'], 'accepted')
    assert resolved['status'] == 'sent'
    clock.advance()
    transport.outcomes.append('crash')
    with pytest.raises(SystemExit):
        core.react(client, message_id, '👀', 'crash-1')
    # A crash after acceptance leaves the attempt in flight; only recovery marks
    # it unknown, exactly like a crashed message send.
    crash_id = rows(core, "SELECT id FROM reaction_outbox WHERE status='sending'")[0][0]
    clock.advance()
    reopened = Core(core.state_dir, transport, clock=clock)
    crashed = reopened.delivery_status(client, crash_id)
    assert (crashed['status'], crashed['error']) == ('unknown', 'interrupted_send')


def test_react_interrupted_attempt_recovers_as_unknown_and_can_retry(env):
    core, transport, clock = env
    client = bind(core, transport)
    message_id = send_published(core, client)
    transport.outcomes.append('rate_limit')
    queued = core.react(client, message_id, '👍', 'recover-1')
    with sqlite3.connect(core.database) as db:
        db.execute("UPDATE reaction_outbox SET status='sending' WHERE id=?", (queued['reaction_id'],))
    reopened = Core(core.state_dir, transport, clock=clock)
    recovered = reopened.delivery_status(client, queued['reaction_id'])
    assert (recovered['status'], recovered['error']) == ('unknown', 'interrupted_send')
    reopened.resolve_unknown(queued['reaction_id'], 'retry')
    clock.advance(40)  # past the reaction scope's 429 backoff
    assert reopened.flush(client_id=client)['reactions'] == 1
    assert reopened.delivery_status(client, queued['reaction_id'])['status'] == 'sent'
    assert transport.reactions[-1]['emoji'] == '👍'


def test_react_refuses_paused_connection_and_disconnect_rejects_queue(env):
    core, transport, clock = env
    client = bind(core, transport)
    message_id = send_published(core, client)
    core.pause(client)
    with pytest.raises(CoreError, match='connection_paused'):
        core.react(client, message_id, '👍', 'paused-1')
    core.pause(client, False)
    transport.outcomes.append('rate_limit')
    queued = core.react(client, message_id, '👍', 'queued-1')
    core.disconnect(client)
    assert rows(core, 'SELECT status,error FROM reaction_outbox WHERE id=?', (queued['reaction_id'],))[0] \
        == ('rejected', 'client_revoked')


# ---------------------------------------------------------------- receiving


def test_reaction_on_own_message_is_delivered_and_can_wake(env):
    from agents_over_telegram import push
    core, transport, clock = env
    push.ensure_tables(core)
    client = bind(core, transport)
    message_id = send_published(core, client)
    collect_one(core, transport, reaction(next_number(transport), message_id,
                                          old=emoji(['👎']), new=emoji(['👍'])))
    # The delivery can wake the agent: it is announced before any poll leases it.
    notice = push.claim_new(core, client, 'watch')
    assert notice['new'] == 1
    assert 'Telegram' in notice['notice']
    assert push.claim_new(core, client, 'hook') is None
    deliveries = core.poll(client)['deliveries']
    assert len(deliveries) == 1
    payload = deliveries[0]
    assert payload['kind'] == 'reaction'
    assert payload['message_id'] == message_id
    assert payload['own_message'] is True
    assert payload['sender'] == {'id': 101, 'is_bot': False, 'username': 'participant'}
    assert payload['old_reaction'] == ['👎']
    assert payload['new_reaction'] == ['👍']
    assert payload['chat_id'] == -100 and payload['thread_id'] is None


def test_reaction_on_foreign_message_is_context_until_owner_widens_wake(env):
    core, transport, clock = env
    client = bind(core, transport)
    collect_one(core, transport, message(next_number(transport), 'human message'))
    foreign = core.poll(client)['deliveries'][0]['message_id']
    own = send_published(core, client)
    collect_one(core, transport, reaction(next_number(transport), foreign, new=emoji(['👍'])))
    collect_one(core, transport, reaction(next_number(transport), own, new=emoji(['❤'])))
    # Default: only the reaction on the agent's own message is delivered; the
    # other stays context, visible in history, never leased or announced.
    assert core.poll(client)['deliveries'][0]['message_id'] == own
    history = [m for m in core.history(client)['messages'] if m.get('kind') == 'reaction']
    assert {m['message_id']: m.get('context_reason') for m in history} == {foreign: 'not_own_message', own: None}
    settings = core.delivery_settings(client, reaction_wake='all')
    assert settings['reaction_wake'] == 'all'
    collect_one(core, transport, reaction(next_number(transport), foreign, old=emoji(['👍']), new=emoji(['🔥'])))
    delivered = core.poll(client)['deliveries']
    assert [d['old_reaction'] for d in delivered if d['kind'] == 'reaction'] == [['👍']]
    core.delivery_settings(client, reaction_wake='none')
    collect_one(core, transport, reaction(next_number(transport), own, old=emoji(['❤']), new=()))
    assert core.poll(client)['deliveries'] == []
    muted = [m for m in core.history(client)['messages']
             if m.get('kind') == 'reaction' and m['message_id'] == own]
    assert {m.get('context_reason') for m in muted} == {None, 'reaction_wake_none'}


def test_reaction_sender_rules_match_messages(env):
    core, transport, clock = env
    client = bind(core, transport)
    message_id = send_published(core, client)
    collect_one(core, transport, reaction(next_number(transport), message_id, sender=999))
    assert 'sender_not_allowed' in unassigned(core)
    assert core.poll(client)['deliveries'] == []
    core.delivery_settings(client, capture_context=True)
    collect_one(core, transport, reaction(next_number(transport), message_id, sender=999, new=emoji(['👀'])))
    assert core.poll(client)['deliveries'] == []
    kept = [m for m in core.history(client)['messages'] if m.get('kind') == 'reaction']
    assert kept and kept[0]['context_reason'] == 'sender_not_allowed'
    assert kept[0]['sender']['id'] == 999


def test_anonymous_and_own_bot_reactions_are_dropped(env):
    core, transport, clock = env
    client = bind(core, transport)
    message_id = send_published(core, client)
    collect_one(core, transport, reaction(next_number(transport), message_id,
                                          actor_chat={'id': -100, 'type': 'supergroup'}))
    update = reaction(next_number(transport), message_id, sender=transport.bot_id)
    collect_one(core, transport, update)
    assert sorted(set(unassigned(core))) == ['shared_bot_echo', 'unverified_sender']
    assert core.poll(client)['deliveries'] == []


def test_reaction_routing_resolves_threads_and_ambiguity(env):
    core, transport, clock = env
    alpha = bind(core, transport, 'Alpha', chat=-100, topic=7)
    beta = bind(core, transport, 'Beta', chat=-100, topic=8)
    core.delivery_settings(alpha, reaction_wake='all')
    core.delivery_settings(beta, reaction_wake='all')
    # Topic messages route through the captured message; a reaction on an
    # unknown message cannot pick a topic, so it stays unassigned.
    collect_one(core, transport, message(next_number(transport), 'to alpha', topic=7))
    alpha_message = core.poll(alpha)['deliveries'][0]['message_id']
    collect_one(core, transport, reaction(next_number(transport), alpha_message, new=emoji(['👍'])))
    assert core.poll(alpha)['deliveries'][0]['kind'] == 'reaction'
    assert core.poll(beta)['deliveries'] == []
    collect_one(core, transport, reaction(next_number(transport), 424242, new=emoji(['👍'])))
    assert 'unresolved_reaction' in unassigned(core)
    # A plain group with one binding accepts reactions on unknown messages too.
    gamma = bind(core, transport, 'Gamma', chat=-300)
    core.delivery_settings(gamma, reaction_wake='all')
    collect_one(core, transport, reaction(next_number(transport), 512345, chat=-300, new=emoji(['👍'])))
    assert core.poll(gamma)['deliveries'][0]['message_id'] == 512345


def test_reaction_on_peer_message_reaches_the_recipient_not_the_author(env):
    core, transport, clock = env
    author = bind(core, transport, 'Author')
    recipient = bind(core, transport, 'Recipient', chat=-200)
    core.grant_peer(author, recipient)
    published = core.send(author, 'for you', 'peer-1', recipient=recipient)
    # The peer message itself is delivered to the recipient as a broker message.
    broker = core.poll(recipient)['deliveries']
    assert broker and broker[0]['origin'] == 'broker'
    collect_one(core, transport, reaction(next_number(transport), published['message_id'],
                                          chat=-200, new=emoji(['👍'])))
    # The peer message is not the recipient agent's own message, so the default
    # wake policy keeps the reaction as context for the recipient; the author's
    # destination never saw it.
    assert core.poll(recipient)['deliveries'] == []
    kept = [m for m in core.history(recipient)['messages'] if m.get('kind') == 'reaction']
    assert kept and kept[0]['own_message'] is False
    assert kept[0]['context_reason'] == 'not_own_message'
    assert not [m for m in core.history(author)['messages'] if m.get('kind') == 'reaction']


def test_custom_and_paid_reactions_are_summarized_without_ids(env):
    core, transport, clock = env
    client = bind(core, transport)
    message_id = send_published(core, client)
    new = [{'type': 'custom_emoji', 'custom_emoji_id': 'x' * 48}, {'type': 'paid'}]
    collect_one(core, transport, reaction(next_number(transport), message_id, new=new))
    payload = core.poll(client)['deliveries'][0]
    assert payload['new_reaction'] == ['custom_emoji', 'paid']
    assert 'custom_emoji_id' not in json.dumps(payload)


def test_reaction_history_covers_context_and_deliveries(env):
    core, transport, clock = env
    client = bind(core, transport)
    own = send_published(core, client)
    collect_one(core, transport, message(next_number(transport), 'human', sender=101))
    core.poll(client)
    incoming = [m for m in core.history(client)['messages']
                if m.get('kind') == 'message' and m.get('origin') == 'telegram']
    foreign = incoming[0]['message_id']
    collect_one(core, transport, reaction(next_number(transport), own, new=emoji(['👍'])))
    collect_one(core, transport, reaction(next_number(transport), foreign, new=emoji(['👀'])))
    core.poll(client)
    reactions = {m['message_id']: m['status'] for m in core.history(client)['messages']
                 if m.get('kind') == 'reaction'}
    assert reactions == {own: 'pending', foreign: 'context'}


# ---------------------------------------------------------------- admin capability


def test_admin_capability_is_tracked_and_reported(env):
    core, transport, clock = env
    client = bind(core, transport)
    status = core.connection_status(client)
    assert status['reactions']['receive']['available'] is None
    assert status['reactions']['wake'] == 'own_messages'
    assert status['reactions']['send']['per_message'] == 1
    # The next collection probes once: a 403 from Telegram means the bot cannot
    # even see the chat, which is recorded as not an administrator.
    collect_one(core, transport, message(next_number(transport), 'hello'))
    status = core.connection_status(client)
    assert status['reactions']['receive']['bot_admin'] is False
    assert 'not an administrator' in status['reactions']['receive']['note']
    transport.chat_members['-100'] = {str(transport.bot_id): {'status': 'administrator'}}
    with sqlite3.connect(core.database) as db:
        db.execute('UPDATE bindings SET bot_admin=NULL')
    collect_one(core, transport, message(next_number(transport), 'again'))
    assert core.connection_status(client)['reactions']['receive']['available'] is True
    # A later demotion update flips it back without another probe.
    transport.updates.append({'update_id': next_number(transport), 'my_chat_member': {
        'chat': {'id': -100}, 'new_chat_member': {'user': {'id': transport.bot_id}, 'status': 'member'}}})
    core.collect()
    assert core.connection_status(client)['reactions']['receive']['available'] is False


def test_human_status_mentions_missing_admin(env):
    from agents_over_telegram.onboarding import human_status
    core, transport, clock = env
    client = bind(core, transport)
    collect_one(core, transport, message(next_number(transport), 'hello'))
    human = human_status(core.connection_status(client))
    assert 'Reactions: not received (the bot is not an administrator here)' in human


# ---------------------------------------------------------------- compatibility


def test_older_state_directory_migrates_and_keeps_working(tmp_path):
    transport = FakeTransport()
    core = Core(tmp_path / 'state', transport, clock=Clock())
    core.register_account(transport.bot_id, transport.username)
    client = bind(core, transport)
    with sqlite3.connect(core.database) as db:
        db.execute('DROP TABLE reaction_outbox')
        db.execute('ALTER TABLE bindings DROP COLUMN bot_admin')
        db.execute('ALTER TABLE bindings DROP COLUMN reaction_wake')
    migrated = Core(core.state_dir, transport, clock=core.clock)
    settings = migrated.delivery_settings(client)
    assert settings['reaction_wake'] == 'own_messages'
    assert migrated.connection_status(client)['reactions']['receive']['bot_admin'] is None
    message_id = send_published(migrated, client)
    assert migrated.react(client, message_id, '👍', 'after-migration')['status'] == 'sent'
    collect_one(migrated, transport, reaction(next_number(transport), message_id, new=emoji(['❤'])))
    assert migrated.poll(client)['deliveries'][0]['kind'] == 'reaction'


def test_transport_requests_reactions_and_sends_one_emoji(tmp_path):
    """The real HTTP transport names message_reaction and sends a well-formed call."""
    import urllib.request
    from agents_over_telegram.transport import TelegramTransport, TransportError, UnknownSend

    captured = []

    class Opener:
        def open(self, request, timeout=0):
            captured.append(request)

            class Response:
                def __init__(self, body):
                    self._body = body

                def read(self, limit=None):
                    return self._body

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

            method = request.full_url.rsplit("/", 1)[-1]
            result = True if method == "setMessageReaction" else []
            return Response(json.dumps({"ok": True, "result": result}).encode())

    transport = TelegramTransport("123456:TEST_TOKEN_abcdefghijklmnop", opener=Opener())
    transport.get_updates(offset=7)
    body = json.loads(captured[-1].data.decode())
    assert body["allowed_updates"] == ["message", "edited_message", "my_chat_member", "message_reaction"]
    transport.set_message_reaction(-100, 42, "👍")
    method = captured[-1].full_url.rsplit("/", 1)[-1]
    body = json.loads(captured[-1].data.decode())
    assert method == "setMessageReaction"
    assert body == {"chat_id": -100, "message_id": 42, "reaction": [{"type": "emoji", "emoji": "👍"}]}
    transport.set_message_reaction(-100, 42, "")
    assert json.loads(captured[-1].data.decode())["reaction"] == []


def test_failed_admin_probe_waits_before_retrying(env):
    core, transport, clock = env
    bind(core, transport, 'Alpha', chat=-100)
    bind(core, transport, 'Beta', chat=-200)
    with sqlite3.connect(core.database) as db:
        db.execute('UPDATE bindings SET bot_admin=NULL,bot_admin_probed=NULL')
    calls = []

    def failing(chat_id, user_id):
        calls.append(chat_id)
        raise TransportError(502)

    transport.get_chat_member = failing
    # Each collection makes at most one probe, so a slow Telegram cannot hold
    # the collector lock once per binding.
    collect_one(core, transport, message(next_number(transport), 'one'))
    assert len(calls) == 1
    collect_one(core, transport, message(next_number(transport), 'two'))
    assert len(calls) == 2 and calls[0] != calls[1]
    # Both probes failed, so neither binding is tried again until the pause ends.
    collect_one(core, transport, message(next_number(transport), 'three'))
    assert len(calls) == 2
    clock.advance(core.BOT_ADMIN_RETRY)
    collect_one(core, transport, message(next_number(transport), 'four'))
    assert len(calls) == 3


def test_reaction_target_is_found_through_the_message_index(env):
    core, transport, clock = env
    alpha = bind(core, transport, 'Alpha', chat=-100, topic=7)
    bind(core, transport, 'Beta', chat=-100, topic=8)
    core.delivery_settings(alpha, reaction_wake='all')
    collect_one(core, transport, message(next_number(transport), 'to alpha', topic=7))
    target = core.poll(alpha)['deliveries'][0]['message_id']
    assert rows(core, 'SELECT tg_chat_id FROM inbox WHERE tg_message_id=?', (target,)) == [(-100,)]
    # An older state directory has no index columns; the migration fills them
    # from the stored payloads, so earlier captured messages still resolve.
    with sqlite3.connect(core.database) as db:
        db.execute('DROP INDEX inbox_telegram_message')
        db.execute('ALTER TABLE inbox DROP COLUMN tg_chat_id')
        db.execute('ALTER TABLE inbox DROP COLUMN tg_message_id')
    migrated = Core(core.state_dir, transport, clock=core.clock)
    assert rows(migrated, 'SELECT tg_chat_id FROM inbox WHERE tg_message_id=?', (target,)) == [(-100,)]
    collect_one(migrated, transport, reaction(next_number(transport), target, new=emoji(['👍'])))
    assert migrated.poll(alpha)['deliveries'][0]['kind'] == 'reaction'
    plan = rows(migrated, 'EXPLAIN QUERY PLAN SELECT client_id FROM inbox WHERE tg_chat_id=? AND tg_message_id=?', (-100, target))
    assert any('inbox_telegram_message' in str(step) for step in plan)


def test_reaction_on_a_context_message_in_a_topic_is_routed(env):
    core, transport, clock = env
    alpha = bind(core, transport, 'Alpha', chat=-100, topic=7)
    bind(core, transport, 'Beta', chat=-100, topic=8)
    core.delivery_settings(alpha, delivery_filter='addressed', capture_context=True, reaction_wake='all')
    number = next_number(transport)
    collect_one(core, transport, message(number, 'not for the bot', topic=7))
    assert core.poll(alpha)['deliveries'] == []
    assert rows(core, 'SELECT context FROM inbox WHERE tg_message_id=?', (number,)) == [(1,)]
    collect_one(core, transport, reaction(next_number(transport), number, new=emoji(['👍'])))
    assert 'unresolved_reaction' not in unassigned(core)
    assert core.poll(alpha)['deliveries'][0]['kind'] == 'reaction'


def test_edit_is_not_matched_to_a_reaction_on_the_same_message(env):
    core, transport, clock = env
    client = bind(core, transport)
    core.delivery_settings(client, delivery_filter='addressed', reaction_wake='all')
    # A reaction on a message AOT never captured is delivered in a plain group.
    collect_one(core, transport, reaction(next_number(transport), 777, new=emoji(['👍'])))
    assert core.poll(client)['deliveries'][0]['kind'] == 'reaction'
    # The author then edits that message without addressing the bot. The
    # reaction must not count as the original delivered message.
    edit = message(next_number(transport), 'edited, not for the bot')
    edit['edited_message'] = dict(edit.pop('message'), message_id=777, edit_date=1001)
    collect_one(core, transport, edit)
    assert core.poll(client)['deliveries'] == []


def test_messages_from_before_a_migration_are_not_matched_in_the_supergroup(env):
    """A supergroup numbers its messages on its own, apart from the old group.

    The old group's messages keep their ids there and are not copied, and the
    old group is deactivated, so nobody can edit or react to them afterwards.
    An edit or reaction in the new supergroup with the same message id is
    about a different message and must not match the captured one.
    """
    core, transport, clock = env
    client = bind(core, transport)
    core.delivery_settings(client, reaction_wake='all')
    original = message(next_number(transport), 'before the upgrade')
    original['message']['chat']['type'] = 'group'
    collect_one(core, transport, original)
    old_id = original['message']['message_id']
    core.delivery_settings(client, delivery_filter='addressed')
    collect_one(core, transport, message(next_number(transport), 'service', migrate_to_chat_id=-1009))
    edit = message(next_number(transport), 'a new supergroup message, edited', chat=-1009)
    edit['edited_message'] = dict(edit.pop('message'), message_id=old_id, edit_date=1001)
    collect_one(core, transport, edit)
    collect_one(core, transport, reaction(next_number(transport), old_id, chat=-1009, new=emoji(['👍'])))
    deliveries = core.poll(client)['deliveries']
    # The edit is not taken for an edit of the delivered message, so the
    # addressed filter keeps it as context instead of delivering it.
    assert [d['kind'] for d in deliveries] == ['message', 'reaction']
    before, react = deliveries
    assert react['chat_id'] == -1009
    assert react['conversation_id'] != before['conversation_id']
    assert rows(core, "SELECT json_extract(payload,'$.context_reason') FROM inbox WHERE context=1") == [('not_addressed',)]
