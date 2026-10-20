"""Private, transactional one-bot routing core. Linux/macOS flock backend.

All local clients of a bot must use the same state directory. This core has no
background threads and never deletes a webhook or configures Telegram accounts.
"""
from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time
from typing import Any

from .transport import TransportError, UnknownSend
from .credentials import CredentialError, private_directory


class CoreError(Exception):
    """Public errors contain only a fixed, non-sensitive code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _bounded(value: Any, maximum: int = 128) -> str:
    return ''.join(c for c in str(value)[:maximum] if c.isprintable())


class Core:
    PAIR_TTL = 600
    MAX_CHAIN = 8
    BOT_PAIR_SEND_LIMIT = 32
    BOT_PAIR_WINDOW = 3600
    UNMATCHED_LIMIT = 100
    BOT_INTERVAL = 0.05
    DESTINATION_INTERVAL = 1.0
    CONVERSATION_INTERVAL = 1.0
    HISTORY_WINDOW = 86400
    HISTORY_CURSOR_TTL = 900

    def __init__(self, state_dir, transport, clock=time.time, queue_limit=1000, *, recover_sends=True):
        if not _integer(queue_limit) or not 1 <= queue_limit <= 100000:
            raise CoreError('invalid_queue_limit')
        try:
            self.state_dir = private_directory(state_dir)
        except CredentialError:
            raise CoreError('insecure_state_directory') from None
        self.transport = transport
        self.clock = clock
        self.queue_limit = queue_limit
        self.database = self.state_dir / 'state.sqlite3'
        try:
            fd = os.open(self.database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            info = self.database.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                raise CoreError('insecure_state_file')
        else:
            os.close(fd)
        with self._db() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS account (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    bot_id INTEGER NOT NULL, username TEXT NOT NULL,
                    cursor INTEGER NOT NULL DEFAULT 0, last_intake REAL,
                    last_attempt REAL, intake_error TEXT, last_gap_at REAL
                );
                CREATE TABLE IF NOT EXISTS clients (
                    id TEXT PRIMARY KEY, label TEXT NOT NULL, secret_hash TEXT UNIQUE NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bindings (
                    client_id TEXT PRIMARY KEY REFERENCES clients(id), chat_id INTEGER NOT NULL,
                    thread_id INTEGER NOT NULL DEFAULT 0, owner_id INTEGER NOT NULL,
                    title TEXT NOT NULL, connected INTEGER NOT NULL DEFAULT 1,
                    paused INTEGER NOT NULL DEFAULT 0, suspended TEXT,
                    created REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS dedicated_destination
                    ON bindings(chat_id,thread_id) WHERE connected=1;
                CREATE TABLE IF NOT EXISTS pairings (
                    id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES clients(id),
                    code_hash TEXT UNIQUE NOT NULL, expires REAL NOT NULL,
                    consumed INTEGER NOT NULL DEFAULT 0, sender_id INTEGER,
                    chat_id INTEGER, thread_id INTEGER, title TEXT, observed REAL
                );
                CREATE TABLE IF NOT EXISTS allowed_senders (
                    client_id TEXT NOT NULL REFERENCES clients(id), sender_id INTEGER NOT NULL,
                    is_bot INTEGER NOT NULL, PRIMARY KEY(client_id,sender_id)
                );
                CREATE TABLE IF NOT EXISTS peer_commands (
                    client_id TEXT PRIMARY KEY REFERENCES clients(id),
                    bot_username TEXT NOT NULL, command TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS grants (
                    source TEXT NOT NULL REFERENCES clients(id), target TEXT NOT NULL REFERENCES clients(id),
                    PRIMARY KEY(source,target)
                );
                CREATE TABLE IF NOT EXISTS inbox (
                    id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES clients(id),
                    event_key TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL,
                    conversation_id TEXT NOT NULL, outbox_id TEXT, event_at REAL,
                    acknowledged INTEGER NOT NULL DEFAULT 0,
                    lease_token TEXT, lease_until REAL,
                    UNIQUE(client_id,event_key)
                );
                CREATE INDEX IF NOT EXISTS inbox_claim ON inbox(client_id,acknowledged,created);
                CREATE TABLE IF NOT EXISTS outbox (
                    id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES clients(id),
                    recipient TEXT, chat_id INTEGER NOT NULL, thread_id INTEGER NOT NULL,
                    idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL,
                    text TEXT NOT NULL, reply_to INTEGER, conversation_id TEXT NOT NULL,
                    status TEXT NOT NULL, error TEXT, message_id INTEGER,
                    created REAL NOT NULL, updated REAL NOT NULL, due REAL NOT NULL,
                    UNIQUE(client_id,idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS rate_limits (
                    scope TEXT PRIMARY KEY, next_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, send_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS bot_exchange_policies (
                    client_id TEXT NOT NULL REFERENCES clients(id), sender_id INTEGER NOT NULL,
                    enabled INTEGER NOT NULL, PRIMARY KEY(client_id,sender_id)
                );
                CREATE TABLE IF NOT EXISTS bot_exchange_members (
                    conversation_id TEXT NOT NULL, client_id TEXT NOT NULL REFERENCES clients(id),
                    sender_id INTEGER NOT NULL,
                    PRIMARY KEY(conversation_id,client_id,sender_id)
                );
                CREATE INDEX IF NOT EXISTS bot_exchange_pair
                    ON bot_exchange_members(client_id,sender_id,conversation_id);
                CREATE INDEX IF NOT EXISTS outbox_exchange_window ON outbox(conversation_id,created);
                CREATE TABLE IF NOT EXISTS history_keys (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), secret BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS unmatched (
                    update_id INTEGER PRIMARY KEY, chat_id INTEGER, reason TEXT NOT NULL, created REAL NOT NULL
                );
            ''')
        with self._tx() as db:
            if 'last_gap_at' not in {row['name'] for row in db.execute('PRAGMA table_info(account)')}:
                db.execute('ALTER TABLE account ADD COLUMN last_gap_at REAL')
            if 'event_at' not in {row['name'] for row in db.execute('PRAGMA table_info(inbox)')}:
                db.execute('ALTER TABLE inbox ADD COLUMN event_at REAL')
            db.execute('INSERT OR IGNORE INTO history_keys(singleton,secret) VALUES(1,?)', (secrets.token_bytes(32),))
            db.execute('CREATE INDEX IF NOT EXISTS inbox_history ON inbox(client_id,COALESCE(event_at,created) DESC)')
            db.execute('CREATE INDEX IF NOT EXISTS outbox_history ON outbox(client_id,created DESC)')
        if recover_sends:
            with self._lock('sender') as acquired:
                if acquired:
                    with self._tx() as db:
                        self._recover_sends(db)

    @contextlib.contextmanager
    def _db(self):
        db = None
        try:
            db = sqlite3.connect(self.database, timeout=5, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA synchronous=FULL')
            yield db
        except sqlite3.Error:
            raise CoreError('storage_error') from None
        finally:
            if db is not None:
                db.close()

    @contextlib.contextmanager
    def _tx(self):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise

    @contextlib.contextmanager
    def _lock(self, name):
        path = self.state_dir / (name + '.lock')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o077 or not stat.S_ISREG(info.st_mode):
                raise CoreError('insecure_lock_file')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                try:
                    yield True
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _client(self, db, client_id):
        row = db.execute('SELECT * FROM clients WHERE id=?', (client_id,)).fetchone()
        if row is None:
            raise CoreError('unknown_client')
        if not row['active']:
            raise CoreError('client_revoked')
        return row

    def _account(self, db):
        row = db.execute('SELECT * FROM account WHERE singleton=1').fetchone()
        if row is None:
            raise CoreError('account_not_registered')
        return row

    def _binding(self, db, client_id, usable=False):
        self._client(db, client_id)
        row = db.execute('SELECT * FROM bindings WHERE client_id=? AND connected=1', (client_id,)).fetchone()
        if row is None:
            raise CoreError('not_connected')
        if usable and row['paused']:
            raise CoreError('connection_paused')
        if usable and row['suspended']:
            raise CoreError('connection_suspended')
        return row

    def register_account(self, bot_id, username):
        if not _integer(bot_id) or bot_id <= 0 or not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_]{3,64}', username):
            raise CoreError('invalid_bot_identity')
        with self._tx() as db:
            previous = db.execute('SELECT * FROM account WHERE singleton=1').fetchone()
            if previous and (previous['bot_id'] != bot_id or previous['username'].lower() != username.lower()):
                raise CoreError('account_already_registered')
            db.execute('INSERT OR IGNORE INTO account(singleton,bot_id,username) VALUES(1,?,?)', (bot_id, username))
        return {'bot_id': bot_id, 'username': username}

    def create_client(self, label):
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 64 or any(not c.isprintable() or c in '[]' for c in label):
            raise CoreError('invalid_label')
        client_id = 'agent_' + secrets.token_hex(12)
        secret = 'aot_' + secrets.token_urlsafe(32)
        with self._tx() as db:
            self._account(db)
            db.execute('INSERT INTO clients(id,label,secret_hash,created) VALUES(?,?,?,?)', (client_id, label.strip(), _digest(secret), self.clock()))
        return {'client_id': client_id, 'client_secret': secret, 'label': label.strip()}

    def authenticate(self, secret):
        if not isinstance(secret, str) or len(secret) > 256:
            raise CoreError('authentication_failed')
        with self._db() as db:
            row = db.execute('SELECT id,active FROM clients WHERE secret_hash=?', (_digest(secret),)).fetchone()
            if row is None or not row['active']:
                raise CoreError('authentication_failed')
            return row['id']

    def connection_begin(self, client_id):
        now = self.clock()
        code = secrets.token_hex(6).upper()
        pairing_id = 'pair_' + secrets.token_hex(12)
        with self._tx() as db:
            self._client(db, client_id)
            account = self._account(db)
            if db.execute('SELECT 1 FROM bindings WHERE client_id=? AND connected=1', (client_id,)).fetchone():
                raise CoreError('already_connected')
            db.execute('UPDATE pairings SET consumed=1 WHERE client_id=?', (client_id,))
            db.execute('INSERT INTO pairings(id,client_id,code_hash,expires) VALUES(?,?,?,?)', (pairing_id, client_id, _digest(code), now + self.PAIR_TTL))
        return {'pairing_id': pairing_id, 'code': code, 'command': f'/pair@{account["username"]} {code}', 'expires_at': now + self.PAIR_TTL}

    def pairing_status(self, pairing_id):
        with self._db() as db:
            row = db.execute('SELECT id,client_id,expires,consumed,sender_id,chat_id,thread_id,title,observed FROM pairings WHERE id=?', (pairing_id,)).fetchone()
            if row is None:
                raise CoreError('pairing_unavailable')
            result = dict(row)
            result['expired'] = row['expires'] <= self.clock()
            return result

    def confirm_pairing(self, pairing_id):
        now = self.clock()
        with self._tx() as db:
            row = db.execute('SELECT * FROM pairings WHERE id=?', (pairing_id,)).fetchone()
            if row is None or row['consumed'] or row['expires'] <= now:
                raise CoreError('pairing_unavailable')
            self._client(db, row['client_id'])
            if row['sender_id'] is None:
                raise CoreError('pairing_not_observed')
            if db.execute('SELECT 1 FROM bindings WHERE connected=1 AND (client_id=? OR (chat_id=? AND thread_id=?))', (row['client_id'], row['chat_id'], row['thread_id'])).fetchone():
                raise CoreError('destination_already_bound')
            db.execute('INSERT INTO bindings(client_id,chat_id,thread_id,owner_id,title,created) VALUES(?,?,?,?,?,?)', (row['client_id'], row['chat_id'], row['thread_id'], row['sender_id'], row['title'], now))
            db.execute('INSERT INTO allowed_senders(client_id,sender_id,is_bot) VALUES(?,?,0)', (row['client_id'], row['sender_id']))
            db.execute('UPDATE pairings SET consumed=1 WHERE id=?', (pairing_id,))
        return self.connection_status(row['client_id'])

    def allow_sender(self, client_id, sender_id, is_bot=False):
        if not _integer(sender_id) or sender_id <= 0 or not isinstance(is_bot, bool):
            raise CoreError('invalid_sender')
        with self._tx() as db:
            self._binding(db, client_id)
            if sender_id == self._account(db)['bot_id']:
                raise CoreError('use_broker_for_shared_bot')
            db.execute('INSERT INTO allowed_senders(client_id,sender_id,is_bot) VALUES(?,?,?) ON CONFLICT(client_id,sender_id) DO UPDATE SET is_bot=excluded.is_bot', (client_id, sender_id, int(is_bot)))
        return {'sender_id': sender_id, 'is_bot': is_bot}

    def configure_peer_command(self, client_id, bot_username, command):
        if not isinstance(bot_username, str) or not re.fullmatch(r'[A-Za-z0-9_]{3,64}', bot_username):
            raise CoreError('invalid_peer_command')
        if not isinstance(command, str) or not re.fullmatch(r'[a-z0-9_]{1,32}', command):
            raise CoreError('invalid_peer_command')
        with self._tx() as db:
            self._binding(db, client_id)
            if bot_username.lower() == self._account(db)['username'].lower():
                raise CoreError('use_broker_for_shared_bot')
            db.execute('INSERT INTO peer_commands(client_id,bot_username,command) VALUES(?,?,?) ON CONFLICT(client_id) DO UPDATE SET bot_username=excluded.bot_username,command=excluded.command', (client_id, bot_username, command))
        return {'bot_username': bot_username, 'command': command}

    def bot_exchange_policy(self, client_id, sender_id, enabled=None):
        """Local operator control; never exposed as an agent MCP mutation."""
        if not _integer(sender_id) or sender_id <= 0 or (enabled is not None and type(enabled) is not bool):
            raise CoreError('invalid_sender')
        with self._tx() as db:
            self._binding(db, client_id)
            if not db.execute('SELECT 1 FROM allowed_senders WHERE client_id=? AND sender_id=? AND is_bot=1',
                              (client_id, sender_id)).fetchone():
                raise CoreError('bot_sender_not_authorized')
            if enabled is not None:
                db.execute('''INSERT INTO bot_exchange_policies VALUES(?,?,?)
                    ON CONFLICT(client_id,sender_id) DO UPDATE SET enabled=excluded.enabled''',
                    (client_id, sender_id, int(enabled)))
                # Include legacy reservations in the rolling budget. Never alter
                # an existing conversation, inbox, outbox or chain counter.
                db.execute('INSERT OR IGNORE INTO bot_exchange_members VALUES(?,?,?)',
                           (f'bot:{client_id}:{sender_id}', client_id, sender_id))
                for admitted in db.execute('SELECT conversation_id,payload FROM inbox WHERE client_id=?', (client_id,)).fetchall():
                    payload = json.loads(admitted['payload'])
                    sender = _mapping(payload.get('sender'))
                    if payload.get('origin') == 'telegram' and sender.get('id') == sender_id and sender.get('is_bot') is True:
                        db.execute('INSERT OR IGNORE INTO bot_exchange_members VALUES(?,?,?)',
                                   (admitted['conversation_id'], client_id, sender_id))
            policy = db.execute('SELECT enabled FROM bot_exchange_policies WHERE client_id=? AND sender_id=?',
                                (client_id, sender_id)).fetchone()
            usage = self._bot_pair_usage(db, client_id, sender_id, self.clock())
        return {'sender_id': sender_id, 'enabled': bool(policy and policy[0]),
                'max_sends_per_exchange': self.MAX_CHAIN, 'pair_send_limit': self.BOT_PAIR_SEND_LIMIT,
                'window_seconds': self.BOT_PAIR_WINDOW, 'reserved_in_window': usage[0],
                'retry_at': usage[1] if usage[0] >= self.BOT_PAIR_SEND_LIMIT else None}

    def _bot_pair_usage(self, db, client_id, sender_id, now):
        stamps = [r[0] for r in db.execute('''SELECT o.created FROM bot_exchange_members p
            JOIN outbox o ON o.conversation_id=p.conversation_id
            WHERE p.client_id=? AND p.sender_id=? AND o.created>?
            ORDER BY o.created DESC''',
            (client_id, sender_id, now - self.BOT_PAIR_WINDOW))]
        # The newest Nth reservation must expire before the next one can fit.
        return len(stamps), (stamps[self.BOT_PAIR_SEND_LIMIT - 1] + self.BOT_PAIR_WINDOW
                             if len(stamps) >= self.BOT_PAIR_SEND_LIMIT else None)

    def unassigned(self, limit=20):
        if not _integer(limit) or not 1 <= limit <= self.UNMATCHED_LIMIT:
            raise CoreError('invalid_unassigned_limit')
        with self._db() as db:
            return {'updates': [dict(row) for row in db.execute('SELECT update_id,chat_id,reason,created FROM unmatched ORDER BY update_id DESC LIMIT ?', (limit,))]}

    def grant_peer(self, source_client_id, target_client_id):
        if source_client_id == target_client_id:
            raise CoreError('invalid_peer')
        with self._tx() as db:
            self._binding(db, source_client_id)
            self._binding(db, target_client_id)
            db.execute('INSERT OR IGNORE INTO grants(source,target) VALUES(?,?)', (source_client_id, target_client_id))
        return {'source': source_client_id, 'target': target_client_id}

    def accounts_status(self, client_id=None):
        with self._db() as db:
            if client_id is not None:
                self._client(db, client_id)
            account = self._account(db)
            return {'accounts': [{'bot_id': account['bot_id'], 'username': account['username'], 'last_intake_at': account['last_intake'], 'intake_error': account['intake_error'], 'last_gap_at': account['last_gap_at'], 'possible_offline_gap': account['last_gap_at'] is not None or (account['last_intake'] is not None and self.clock() - account['last_intake'] >= 86400)}]}

    def connection_status(self, client_id):
        """Read scoped connection facts and fixed guidance without intake or writes."""
        now = self.clock()
        with self._db() as db:
            client = self._client(db, client_id)
            account = self._account(db)
            row = db.execute('SELECT * FROM bindings WHERE client_id=? AND connected=1', (client_id,)).fetchone()
            pairing = db.execute('SELECT id,expires,sender_id,chat_id,thread_id,title FROM pairings WHERE client_id=? AND consumed=0 AND expires>? ORDER BY expires DESC LIMIT 1', (client_id, now)).fetchone()
            pending = db.execute('SELECT count(*) FROM inbox WHERE client_id=? AND acknowledged=0', (client_id,)).fetchone()[0]
            counts = {r['status']: r['n'] for r in db.execute('SELECT status,count(*) n FROM outbox WHERE client_id=? GROUP BY status', (client_id,))}
            peers = [dict(r) for r in db.execute('SELECT c.id AS client_id,c.label FROM grants g JOIN clients c ON c.id=g.target WHERE g.source=? AND c.active=1', (client_id,))]
            senders = [{'sender_id': r['sender_id'], 'is_bot': bool(r['is_bot'])}
                       for r in db.execute('SELECT sender_id,is_bot FROM allowed_senders WHERE client_id=? ORDER BY sender_id', (client_id,))]
            gap = account['last_gap_at'] is not None or (account['last_intake'] is not None and now - account['last_intake'] >= self.HISTORY_WINDOW)
            if row is None:
                if pairing is None:
                    state, action = 'unpaired', 'begin_pairing'
                    message = "Connect this agent to a Telegram group or topic using the owner's existing shared bot."
                elif pairing['sender_id'] is None:
                    state, action = 'pair_pending', 'send_pairing_command'
                    message = 'Send the pairing command from connection setup in the chosen Telegram group or topic.'
                else:
                    state, action = 'pair_pending', 'confirm_pairing'
                    message = 'Review the observed sender, group and topic locally, then confirm pairing.'
            elif row['suspended']:
                state, action = 'suspended', 'review_connection'
                message = 'Ask the local owner to review this suspended Telegram destination before continuing.'
            elif row['paused']:
                state, action = 'paused', 'resume_connection'
                message = 'Resume this connection when you want to handle messages again; queued work is preserved.'
            else:
                state = 'connected'
                if counts.get('unknown', 0):
                    action = 'review_unknown_delivery'
                    message = 'Review the uncertain outgoing delivery locally before attempting to resend it.'
                elif account['intake_error']:
                    action = 'review_intake'
                    message = 'Ask the local owner to review the intake error; already captured messages remain available.'
                elif pending:
                    action = 'poll_messages'
                    message = 'Check the inbox to handle pending messages, then acknowledge only completed work.'
                elif gap:
                    action = 'review_gap'
                    message = 'Captured history may have a gap. Check for new messages when ready and allow for missing context.'
                else:
                    action = 'check_messages'
                    message = 'Check for new messages when ready. For automatic wakeups, ask to listen for Telegram in this chat for a chosen duration; listener status is not checked here.'
            return {
                'client_id': client_id, 'label': client['label'],
                'connected': row is not None, 'paused': bool(row['paused']) if row else False,
                'suspended': row['suspended'] if row else None,
                'destination': {'chat_id': row['chat_id'], 'thread_id': row['thread_id'] or None,
                                'title': row['title'], 'owner_id': row['owner_id']} if row else None,
                'pairing': dict(pairing) if pairing else None, 'pending_messages': pending,
                'outbox': counts, 'permitted_peers': peers,
                'bot': {'id': account['bot_id'], 'username': account['username']},
                'allowed_senders': senders,
                'intake': {'last_success_at': account['last_intake'], 'last_attempt_at': account['last_attempt'],
                           'error': account['intake_error'], 'last_gap_at': account['last_gap_at'],
                           'possible_offline_gap': bool(gap)},
                'delivery': {'mode': 'on_demand', 'automatic': False, 'scope': 'inbox_core',
                             'listener_status': 'not_checked'},
                'history': {'window_hours': self.HISTORY_WINDOW // 3600, 'kind': 'captured_only'},
                'state': state, 'next_action': {'code': action, 'message': message},
            }

    def _unmatched(self, db, update_id, chat_id, reason, now):
        db.execute('INSERT OR IGNORE INTO unmatched(update_id,chat_id,reason,created) VALUES(?,?,?,?)', (update_id, chat_id, reason, now))
        db.execute('DELETE FROM unmatched WHERE update_id NOT IN (SELECT update_id FROM unmatched ORDER BY update_id DESC LIMIT ?)', (self.UNMATCHED_LIMIT,))

    def _enqueue(self, db, client_id, event_key, payload, conversation_id, now, outbox_id=None, event_at=None):
        if db.execute('SELECT 1 FROM inbox WHERE client_id=? AND event_key=?', (client_id, event_key)).fetchone():
            return
        count = db.execute('SELECT count(*) FROM inbox WHERE client_id=? AND acknowledged=0', (client_id,)).fetchone()[0]
        if count >= self.queue_limit:
            raise CoreError('queue_full')
        db.execute('INSERT INTO inbox(id,client_id,event_key,payload,created,conversation_id,outbox_id,event_at) VALUES(?,?,?,?,?,?,?,?)', ('msg_' + secrets.token_hex(12), client_id, event_key, json.dumps(payload, ensure_ascii=False), now, conversation_id, outbox_id, now if event_at is None else event_at))

    def _route_update(self, db, update, account, now):
        update_id = update['update_id']
        membership = update.get('my_chat_member')
        if isinstance(membership, dict):
            chat_id = _mapping(membership.get('chat')).get('id')
            new = _mapping(membership.get('new_chat_member'))
            if _integer(chat_id) and _mapping(new.get('user')).get('id') == account['bot_id'] and (new.get('status') in ('left', 'kicked') or (new.get('status') == 'restricted' and (not new.get('is_member', True) or not new.get('can_send_messages', True)))):
                db.execute('UPDATE bindings SET suspended=? WHERE chat_id=? AND connected=1', ('bot_removed_or_restricted', chat_id))
            return
        kind = 'edited_message' if 'edited_message' in update else 'message'
        msg = update.get(kind)
        if not isinstance(msg, dict):
            self._unmatched(db, update_id, None, 'unsupported_update', now)
            return
        chat = _mapping(msg.get('chat'))
        chat_id = chat.get('id')
        thread_id = msg.get('message_thread_id', 0)
        if not _integer(chat_id) or not _integer(thread_id) or thread_id < 0 or chat.get('type') not in ('group', 'supergroup'):
            self._unmatched(db, update_id, None, 'unsupported_destination', now)
            return
        if _integer(msg.get('migrate_to_chat_id')):
            db.execute('UPDATE bindings SET suspended=? WHERE chat_id=? AND connected=1', ('group_migrated:' + str(msg['migrate_to_chat_id']), chat_id))
            return
        if 'forum_topic_closed' in msg or 'forum_topic_edited' in msg:
            db.execute('UPDATE bindings SET suspended=? WHERE chat_id=? AND thread_id=? AND connected=1', ('topic_changed', chat_id, thread_id))
            return
        sender = _mapping(msg.get('from'))
        sender_id = sender.get('id')
        text = msg.get('text')
        if not _integer(sender_id) or sender_id <= 0 or not isinstance(sender.get('is_bot'), bool) or msg.get('sender_chat'):
            self._unmatched(db, update_id, chat_id, 'unverified_sender', now)
            return
        if sender_id == account['bot_id']:
            self._unmatched(db, update_id, chat_id, 'shared_bot_echo', now)
            return
        if not isinstance(text, str) or len(text) > 4096 or not _integer(msg.get('message_id')):
            self._unmatched(db, update_id, chat_id, 'unsupported_message', now)
            return
        command = re.fullmatch(r'/pair@([A-Za-z0-9_]+) ([A-F0-9]{12})', text)
        if command and command.group(1).lower() == account['username'].lower():
            if not sender['is_bot'] and kind == 'message':
                db.execute('UPDATE pairings SET sender_id=?,chat_id=?,thread_id=?,title=?,observed=? WHERE code_hash=? AND consumed=0 AND expires>? AND sender_id IS NULL', (sender_id, chat_id, thread_id, _bounded(chat.get('title', ''), 128), now, _digest(command.group(2)), now))
            return
        binding = db.execute('SELECT b.* FROM bindings b JOIN clients c ON c.id=b.client_id WHERE b.chat_id=? AND b.thread_id=? AND b.connected=1 AND c.active=1', (chat_id, thread_id)).fetchone()
        if binding is None:
            self._unmatched(db, update_id, chat_id, 'unbound_destination', now)
            return
        if not db.execute('SELECT 1 FROM allowed_senders WHERE client_id=? AND sender_id=? AND is_bot=?', (binding['client_id'], sender_id, int(sender['is_bot']))).fetchone():
            self._unmatched(db, update_id, chat_id, 'sender_not_allowed', now)
            return
        if not sender['is_bot'] and sender_id == binding['owner_id'] and text.lower() == f'/pause@{account["username"]}'.lower() and kind == 'message':
            db.execute('UPDATE bindings SET paused=1 WHERE client_id=?', (binding['client_id'],))
            return
        # A human message starts its own conversation unless it replies to a known
        # outbound message. Bot replies inherit a known conversation; unsolicited
        # bot messages keep the legacy chain unless the operator enables bounded
        # roots. Reply lineage and edits cannot mint another exchange budget.
        reply_to = _mapping(msg.get('reply_to_message')).get('message_id')
        known = db.execute('SELECT conversation_id FROM outbox WHERE chat_id=? AND thread_id=? AND message_id=? AND status=\'sent\' ORDER BY created DESC LIMIT 1', (chat_id, thread_id, reply_to)).fetchone() if _integer(reply_to) else None
        if known:
            conversation = known['conversation_id']
        elif sender['is_bot']:
            conversation = f'bot:{binding["client_id"]}:{sender_id}'
            policy = db.execute('SELECT enabled FROM bot_exchange_policies WHERE client_id=? AND sender_id=?',
                                (binding['client_id'], sender_id)).fetchone()
            if policy and policy[0] and reply_to is None and kind == 'message':
                conversation = f'bot-root:{binding["client_id"]}:{sender_id}:{msg["message_id"]}'
        elif sender_id == binding['owner_id']:
            conversation = f'tg:{chat_id}:{thread_id}:{msg["message_id"]}'
        else:
            conversation = f'participant:{binding["client_id"]}:{sender_id}'
        if sender['is_bot'] and kind == 'edited_message':
            for original in db.execute('SELECT payload,conversation_id FROM inbox WHERE client_id=? ORDER BY created,rowid',
                                       (binding['client_id'],)):
                prior = json.loads(original['payload'])
                if (prior.get('origin') == 'telegram' and prior.get('message_id') == msg['message_id']
                        and _mapping(prior.get('sender')).get('id') == sender_id):
                    conversation = original['conversation_id']
                    break
        if sender['is_bot'] and db.execute('''SELECT 1 FROM bot_exchange_policies
                WHERE client_id=? AND sender_id=? AND enabled=1''',
                (binding['client_id'], sender_id)).fetchone():
            db.execute('INSERT OR IGNORE INTO bot_exchange_members VALUES(?,?,?)',
                       (conversation, binding['client_id'], sender_id))
        payload = {'origin': 'telegram', 'kind': 'edited' if kind == 'edited_message' else 'message', 'update_id': update_id, 'message_id': msg['message_id'], 'chat_id': chat_id, 'thread_id': thread_id or None, 'sender': {'id': sender_id, 'is_bot': sender['is_bot'], 'username': _bounded(sender.get('username', ''), 64)}, 'text': text, 'reply_to': reply_to if _integer(reply_to) else None}
        stamp = msg.get('edit_date' if kind == 'edited_message' else 'date')
        event_at = stamp if _integer(stamp) and 0 < stamp <= now else now
        self._enqueue(db, binding['client_id'], f'tg:{update_id}', payload, conversation, now, event_at=event_at)

    def collect(self, *, timeout=0):
        if not _integer(timeout) or not 0 <= timeout <= 20:
            raise CoreError('invalid_collect_timeout')
        with self._lock('collector') as acquired:
            if not acquired:
                return {'status': 'busy', 'updates': 0}
            with self._db() as db:
                account = self._account(db)
                cursor = account['cursor']
            try:
                webhook = self.transport.get_webhook_info()
                if not isinstance(webhook, dict) or not isinstance(webhook.get('url'), str):
                    raise CoreError('invalid_transport_response')
                if webhook.get('url'):
                    raise CoreError('webhook_conflict')
                updates = self.transport.get_updates(offset=cursor, timeout=timeout)
                if not isinstance(updates, list) or len(updates) > 100:
                    raise CoreError('invalid_transport_response')
                if any(not isinstance(u, dict) or not _integer(u.get('update_id')) or u['update_id'] < 0 for u in updates):
                    raise CoreError('invalid_transport_response')
                with self._tx() as db:
                    accepted = 0
                    for update in sorted(updates, key=lambda u: u['update_id']):
                        if update['update_id'] < cursor:
                            continue
                        self._route_update(db, update, account, self.clock())
                        cursor = update['update_id'] + 1
                        accepted += 1
                    db.execute('UPDATE account SET cursor=?,last_gap_at=CASE WHEN last_intake IS NOT NULL AND ?-last_intake>=86400 THEN ? ELSE last_gap_at END,last_intake=?,last_attempt=?,intake_error=NULL WHERE singleton=1', (cursor, self.clock(), self.clock(), self.clock(), self.clock()))
                return {'status': 'ok', 'updates': accepted, 'cursor': cursor}
            except (TransportError, CoreError) as error:
                code = error.code if isinstance(error, CoreError) else 'telegram_intake_failed'
                with self._tx() as db:
                    db.execute('UPDATE account SET last_attempt=?,intake_error=? WHERE singleton=1', (self.clock(), code))
                raise CoreError(code) from None

    def poll(self, client_id, limit=20, lease_seconds=60):
        if not _integer(limit) or not 1 <= limit <= 100 or not _integer(lease_seconds) or not 1 <= lease_seconds <= 3600:
            raise CoreError('invalid_poll_options')
        now = self.clock()
        token = secrets.token_urlsafe(24)
        with self._tx() as db:
            binding = self._binding(db, client_id)
            if binding['paused'] or binding['suspended']:
                return {'deliveries': [], 'lease_token': None, 'paused': bool(binding['paused']), 'suspended': binding['suspended']}
            rows = db.execute('SELECT * FROM inbox WHERE client_id=? AND acknowledged=0 AND (lease_until IS NULL OR lease_until<=?) ORDER BY created,rowid LIMIT ?', (client_id, now, limit)).fetchall()
            deliveries = []
            for row in rows:
                db.execute('UPDATE inbox SET lease_token=?,lease_until=? WHERE id=?', (token, now + lease_seconds, row['id']))
                delivery = json.loads(row['payload'])
                delivery.update(delivery_id=row['id'], conversation_id=row['conversation_id'], received_at=row['created'])
                if row['outbox_id']:
                    published = db.execute('SELECT status,message_id FROM outbox WHERE id=?', (row['outbox_id'],)).fetchone()
                    delivery['publication'] = {'outbox_id': row['outbox_id'], 'status': published['status'], 'message_id': published['message_id']}
                deliveries.append(delivery)
        return {'deliveries': deliveries, 'lease_token': token if deliveries else None, 'lease_expires_at': now + lease_seconds if deliveries else None, 'paused': False, 'suspended': None}

    @staticmethod
    def _history_encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')

    @staticmethod
    def _history_decode(value):
        return base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)

    def _history_cursor(self, snapshot, key):
        encoded = self._history_encode(json.dumps(snapshot, separators=(',', ':'), allow_nan=False).encode())
        signature = self._history_encode(hmac.digest(key, encoded.encode('ascii'), 'sha256'))
        return encoded + '.' + signature

    def _history_snapshot(self, cursor, key, client_id, now):
        try:
            encoded, signature = cursor.split('.')
            expected = hmac.digest(key, encoded.encode('ascii'), 'sha256')
            if not hmac.compare_digest(expected, self._history_decode(signature)):
                raise ValueError()
            snapshot = json.loads(self._history_decode(encoded))
            if not isinstance(snapshot, dict) or set(snapshot) != {'v', 'client', 'at', 'inbox_max', 'outbox_max', 'last'}:
                raise ValueError()
            if snapshot['v'] != 1 or snapshot['client'] != client_id:
                raise ValueError()
            at = snapshot['at']
            if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at):
                raise ValueError()
            for field in ('inbox_max', 'outbox_max'):
                if not _integer(snapshot[field]) or not 0 <= snapshot[field] <= 9223372036854775807:
                    raise ValueError()
            last = snapshot['last']
            if not isinstance(last, list) or len(last) != 3:
                raise ValueError()
            stamp, stream, sequence = last
            if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or not at - self.HISTORY_WINDOW <= stamp <= at:
                raise ValueError()
            if not _integer(stream) or stream not in (0, 1) or not _integer(sequence) or not 1 <= sequence <= snapshot['inbox_max' if stream == 0 else 'outbox_max']:
                raise ValueError()
        except (ValueError, TypeError, UnicodeError, OverflowError):
            raise CoreError('history_cursor_invalid') from None
        if now < at or now >= at + self.HISTORY_CURSOR_TTL:
            raise CoreError('history_cursor_expired')
        return snapshot

    def history(self, client_id, limit=20, cursor=None):
        """Read captured context without claiming or acknowledging any work.

        A cursor freezes the event window and row membership for 15 minutes.
        Acknowledgment and publication statuses reflect the time of each read.
        """
        if not _integer(limit) or not 1 <= limit <= 50:
            raise CoreError('invalid_history_options')
        if cursor is not None and (not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048):
            raise CoreError('history_cursor_invalid')
        now = self.clock()
        with self._db() as db:
            # A deferred, query-only transaction provides a consistent page without
            # acquiring the write lock or changing cursor, leases or send recovery.
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            binding = self._binding(db, client_id)
            client = self._client(db, client_id)
            account = self._account(db)
            key = db.execute('SELECT secret FROM history_keys WHERE singleton=1').fetchone()['secret']
            if cursor is None:
                snapshot = {'v': 1, 'client': client_id, 'at': now,
                            'inbox_max': db.execute('SELECT COALESCE(MAX(rowid),0) FROM inbox WHERE client_id=?', (client_id,)).fetchone()[0],
                            'outbox_max': db.execute('SELECT COALESCE(MAX(rowid),0) FROM outbox WHERE client_id=?', (client_id,)).fetchone()[0],
                            'last': None}
            else:
                snapshot = self._history_snapshot(cursor, key, client_id, now)
            start, end = snapshot['at'] - self.HISTORY_WINDOW, snapshot['at']
            query = """
                WITH events AS (
                    SELECT 0 AS stream,rowid AS sequence,id,COALESCE(event_at,created) AS stamp
                    FROM inbox WHERE client_id=? AND rowid<=? AND COALESCE(event_at,created)>=? AND COALESCE(event_at,created)<=?
                    UNION ALL
                    SELECT 1 AS stream,rowid AS sequence,id,created AS stamp
                    FROM outbox WHERE client_id=? AND rowid<=? AND created>=? AND created<=?
                ) SELECT * FROM events
            """
            params = [client_id, snapshot['inbox_max'], start, end, client_id, snapshot['outbox_max'], start, end]
            if snapshot['last'] is not None:
                query += ' WHERE (stamp,stream,sequence)<(?,?,?)'
                params.extend(snapshot['last'])
            query += ' ORDER BY stamp DESC,stream DESC,sequence DESC LIMIT ?'
            params.append(limit + 1)
            events = db.execute(query, params).fetchall()
            messages = []
            for event in events[:limit]:
                if event['stream'] == 0:
                    row = db.execute('SELECT * FROM inbox WHERE id=? AND client_id=?', (event['id'], client_id)).fetchone()
                    message = json.loads(row['payload'])
                    message.update(direction='incoming', delivery_id=row['id'], conversation_id=row['conversation_id'],
                                   status='acknowledged' if row['acknowledged'] else 'pending',
                                   event_at=event['stamp'], captured_at=row['created'], context_only=True)
                    if row['outbox_id']:
                        publication = db.execute('SELECT status,message_id FROM outbox WHERE id=?', (row['outbox_id'],)).fetchone()
                        message['publication'] = {'outbox_id': row['outbox_id'], 'status': publication['status'], 'message_id': publication['message_id']}
                else:
                    row = db.execute('SELECT * FROM outbox WHERE id=? AND client_id=?', (event['id'], client_id)).fetchone()
                    message = self._outbox_status(row)
                    message.update(origin='outgoing', direction='outgoing', kind='message', text=row['text'],
                                   logical_sender={'client_id': client_id, 'label': client['label']},
                                   actual_bot={'id': account['bot_id'], 'username': account['username']},
                                   chat_id=row['chat_id'], thread_id=row['thread_id'] or None, reply_to=row['reply_to'],
                                   event_at=event['stamp'], captured_at=row['created'], context_only=True)
                messages.append(message)
            next_cursor = None
            if len(events) > limit:
                last = events[limit - 1]
                snapshot['last'] = [last['stamp'], last['stream'], last['sequence']]
                next_cursor = self._history_cursor(snapshot, key)
            return {'context_only': True, 'messages': messages, 'next_cursor': next_cursor,
                    'coverage': {'kind': 'captured_only', 'description': 'Captured messages only; this does not backfill Telegram history.',
                                 'window_start': start, 'window_end': end, 'window_seconds': self.HISTORY_WINDOW,
                                 'snapshot_expires_at': end + self.HISTORY_CURSOR_TTL, 'statuses_as_of': now,
                                 'timestamp_policy': 'Telegram event time when valid; otherwise capture time.',
                                 'connected_at': binding['created'], 'last_gap_at': account['last_gap_at'],
                                 'possible_offline_gap': account['last_gap_at'] is not None or (account['last_intake'] is not None and now - account['last_intake'] >= self.HISTORY_WINDOW)}}

    def ack(self, client_id, delivery_ids, lease_token):
        if not isinstance(delivery_ids, list) or not 1 <= len(delivery_ids) <= 100 or any(not isinstance(i, str) for i in delivery_ids) or not isinstance(lease_token, str) or len(lease_token) > 128:
            raise CoreError('invalid_ack')
        ids = list(dict.fromkeys(delivery_ids))
        now = self.clock()
        with self._tx() as db:
            self._binding(db, client_id)
            for delivery_id in ids:
                row = db.execute('SELECT * FROM inbox WHERE id=? AND client_id=?', (delivery_id, client_id)).fetchone()
                if row is None or row['lease_token'] != lease_token or (not row['acknowledged'] and row['lease_until'] <= now):
                    raise CoreError('invalid_delivery_lease')
            for delivery_id in ids:
                db.execute('UPDATE inbox SET acknowledged=1 WHERE id=? AND client_id=?', (delivery_id, client_id))
        return {'acknowledged': ids}

    def pause(self, client_id, paused=True):
        if not isinstance(paused, bool):
            raise CoreError('invalid_pause')
        with self._tx() as db:
            binding = self._binding(db, client_id)
            if not paused and binding['suspended']:
                raise CoreError('connection_suspended')
            db.execute('UPDATE bindings SET paused=? WHERE client_id=?', (int(paused), client_id))
        return self.connection_status(client_id)

    def disconnect(self, client_id):
        with self._tx() as db:
            self._client(db, client_id)
            db.execute('UPDATE bindings SET connected=0 WHERE client_id=?', (client_id,))
            db.execute('UPDATE clients SET active=0 WHERE id=?', (client_id,))
            db.execute('UPDATE pairings SET consumed=1 WHERE client_id=?', (client_id,))
            db.execute('UPDATE outbox SET status=\'rejected\',error=\'client_revoked\',updated=? WHERE status=\'queued\' AND (client_id=? OR recipient=?)', (self.clock(), client_id, client_id))
        return {'client_id': client_id, 'connected': False, 'revoked': True, 'queued_work_preserved': True}

    def _conversation(self, db, client_id, conversation_id):
        if conversation_id is not None:
            if not isinstance(conversation_id, str) or len(conversation_id) > 200:
                raise CoreError('invalid_conversation')
            if not db.execute('SELECT 1 FROM inbox WHERE client_id=? AND conversation_id=? UNION SELECT 1 FROM outbox WHERE client_id=? AND conversation_id=? LIMIT 1', (client_id, conversation_id, client_id, conversation_id)).fetchone():
                raise CoreError('unknown_conversation')
            return conversation_id
        # Continue the most recent admitted conversation, including broker work.
        # Explicit conversation IDs always retain their own durable chain budget.
        row = db.execute('SELECT conversation_id FROM inbox WHERE client_id=? ORDER BY created DESC,rowid DESC LIMIT 1', (client_id,)).fetchone()
        if row:
            return row['conversation_id']
        return 'local:' + client_id

    def send(self, client_id, text, idempotency_key, recipient=None, reply_to=None, conversation_id=None):
        if not isinstance(text, str) or not 1 <= len(text) <= 3500:
            raise CoreError('invalid_text')
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
            raise CoreError('invalid_idempotency_key')
        if reply_to is not None and (not _integer(reply_to) or reply_to <= 0):
            raise CoreError('invalid_reply')
        if recipient is not None and (not isinstance(recipient, str) or len(recipient) > 100):
            raise CoreError('invalid_peer')
        request_hash = _digest(json.dumps([text, recipient, reply_to, conversation_id]))
        now = self.clock()
        with self._tx() as db:
            client = self._client(db, client_id)
            existing = db.execute('SELECT * FROM outbox WHERE client_id=? AND idempotency_key=?', (client_id, idempotency_key)).fetchone()
            if existing:
                if existing['request_hash'] != request_hash:
                    raise CoreError('idempotency_conflict')
                outbox_id = existing['id']
            else:
                binding = self._binding(db, client_id, usable=True)
                account = self._account(db)
                if recipient is not None:
                    if not db.execute('SELECT 1 FROM grants WHERE source=? AND target=?', (client_id, recipient)).fetchone():
                        raise CoreError('peer_not_authorized')
                    binding = self._binding(db, recipient, usable=True)
                reply_conversation = None
                if reply_to is not None:
                    for row in db.execute('SELECT payload,conversation_id FROM inbox WHERE client_id=? ORDER BY created,rowid',
                                          (recipient or client_id,)):
                        if json.loads(row['payload']).get('message_id') == reply_to:
                            reply_conversation = row['conversation_id']
                            break
                    if reply_conversation is None:
                        row = db.execute('SELECT conversation_id FROM outbox WHERE chat_id=? AND thread_id=? AND message_id=? AND status=\'sent\'',
                                         (binding['chat_id'], binding['thread_id'], reply_to)).fetchone()
                        if row:
                            reply_conversation = row['conversation_id']
                    if reply_conversation is None:
                        raise CoreError('unknown_reply')
                conversation = self._conversation(db, client_id, conversation_id if conversation_id is not None else reply_conversation)
                if reply_conversation is not None and reply_conversation != conversation:
                    raise CoreError('reply_conversation_mismatch')
                db.execute('INSERT OR IGNORE INTO conversations(id) VALUES(?)', (conversation,))
                count = db.execute('SELECT send_count FROM conversations WHERE id=?', (conversation,)).fetchone()[0]
                if count >= self.MAX_CHAIN:
                    raise CoreError('conversation_limit')
                for pair in db.execute('SELECT client_id,sender_id FROM bot_exchange_members WHERE conversation_id=?',
                                       (conversation,)).fetchall():
                    if self._bot_pair_usage(db, pair['client_id'], pair['sender_id'], now)[0] >= self.BOT_PAIR_SEND_LIMIT:
                        raise CoreError('bot_pair_limit')
                outbox_id = 'out_' + secrets.token_hex(12)
                publication = f'[Agent: {client["label"]} | shared bot @{account["username"]}]\n{text}'
                peer = db.execute('SELECT * FROM peer_commands WHERE client_id=?', (client_id,)).fetchone() if recipient is None else None
                if peer:
                    publication = f'/{peer["command"]}@{peer["bot_username"]}\n' + publication
                db.execute('INSERT INTO outbox(id,client_id,recipient,chat_id,thread_id,idempotency_key,request_hash,text,reply_to,conversation_id,status,created,updated,due) VALUES(?,?,?,?,?,?,?,?,?,?,\'queued\',?,?,?)', (outbox_id, client_id, recipient, binding['chat_id'], binding['thread_id'], idempotency_key, request_hash, publication, reply_to, conversation, now, now, now))
                db.execute('UPDATE conversations SET send_count=send_count+1 WHERE id=?', (conversation,))
                if recipient:
                    payload = {'origin': 'broker', 'kind': 'message', 'text': text, 'logical_sender': {'client_id': client_id, 'label': client['label']}, 'actual_bot': {'id': account['bot_id'], 'username': account['username']}, 'chat_id': binding['chat_id'], 'thread_id': binding['thread_id'] or None}
                    self._enqueue(db, recipient, 'broker:' + outbox_id, payload, conversation, now, outbox_id)
        self.flush(client_id=client_id, limit=20)
        return self.delivery_status(client_id, outbox_id)

    def _outbox_status(self, row):
        return {'outbox_id': row['id'], 'status': row['status'], 'error': row['error'], 'message_id': row['message_id'], 'recipient': row['recipient'], 'conversation_id': row['conversation_id'], 'created_at': row['created'], 'updated_at': row['updated'], 'retry_at': row['due'] if row['status'] == 'queued' else None}

    def delivery_status(self, client_id, outbox_id):
        with self._db() as db:
            self._client(db, client_id)
            row = db.execute('SELECT * FROM outbox WHERE client_id=? AND id=?', (client_id, outbox_id)).fetchone()
            if row is None:
                raise CoreError('unknown_delivery')
            return self._outbox_status(row)

    def _recover_sends(self, db):
        # Only call while holding sender.lock: no live local sender can own a row.
        db.execute('UPDATE outbox SET status=\'unknown\',error=\'interrupted_send\',updated=? WHERE status=\'sending\'', (self.clock(),))

    def flush(self, client_id=None, limit=20):
        if not _integer(limit) or not 1 <= limit <= 100:
            raise CoreError('invalid_flush_limit')
        if client_id is not None:
            with self._db() as db:
                self._client(db, client_id)
        with self._lock('sender') as acquired:
            if not acquired:
                return {'status': 'busy', 'attempted': 0}
            with self._tx() as db:
                self._recover_sends(db)
                rows = db.execute('SELECT id FROM outbox WHERE status=\'queued\' AND due<=? AND (? IS NULL OR client_id=?) ORDER BY created,rowid LIMIT ?', (self.clock(), client_id, client_id, limit)).fetchall()
            attempted = 0
            for candidate in rows:
                with self._tx() as db:
                    row = db.execute('SELECT * FROM outbox WHERE id=?', (candidate['id'],)).fetchone()
                    if row['status'] != 'queued':
                        continue
                    try:
                        self._binding(db, row['client_id'], usable=True)
                        if row['recipient']:
                            self._binding(db, row['recipient'], usable=True)
                    except CoreError:
                        continue
                    now = self.clock()
                    scopes = [('bot', self.BOT_INTERVAL), (f'dest:{row["chat_id"]}:{row["thread_id"]}', self.DESTINATION_INTERVAL), ('conv:' + row['conversation_id'], self.CONVERSATION_INTERVAL)]
                    due = now
                    for scope, _ in scopes:
                        rate = db.execute('SELECT next_at FROM rate_limits WHERE scope=?', (scope,)).fetchone()
                        if rate:
                            due = max(due, rate['next_at'])
                    if due > now:
                        db.execute('UPDATE outbox SET due=? WHERE id=?', (due, row['id']))
                        continue
                    for scope, interval in scopes:
                        db.execute('INSERT INTO rate_limits(scope,next_at) VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET next_at=excluded.next_at', (scope, now + interval))
                    db.execute('UPDATE outbox SET status=\'sending\',updated=? WHERE id=?', (now, row['id']))
                attempted += 1
                try:
                    response = self.transport.send_message(chat_id=row['chat_id'], text=row['text'], thread_id=row['thread_id'] or None, reply_to=row['reply_to'])
                    if not isinstance(response, dict) or not _integer(response.get('message_id')) or response['message_id'] <= 0:
                        raise UnknownSend()
                except UnknownSend:
                    status, error, message_id, due = 'unknown', 'uncertain_send', None, self.clock()
                except TransportError as exc:
                    if str(exc.code) in ('429', 'rate_limited', 'telegram_429'):
                        wait = exc.retry_after if isinstance(exc.retry_after, (int, float)) and not isinstance(exc.retry_after, bool) and math.isfinite(exc.retry_after) and exc.retry_after > 0 else 60
                        due = self.clock() + min(wait, 86400)
                        status, error, message_id = 'queued', 'rate_limited', None
                    elif isinstance(exc.code, int) and 400 <= exc.code < 500:
                        status, error, message_id, due = 'rejected', 'telegram_rejected', None, self.clock()
                    else:
                        status, error, message_id, due = 'unknown', 'transport_failure', None, self.clock()
                except Exception:
                    # An unexpected adapter failure after entering send may follow
                    # remote acceptance. Never make a speculative automatic retry.
                    status, error, message_id, due = 'unknown', 'transport_failure', None, self.clock()
                else:
                    status, error, message_id, due = 'sent', None, response['message_id'], self.clock()
                with self._tx() as db:
                    db.execute('UPDATE outbox SET status=?,error=?,message_id=?,due=?,updated=? WHERE id=?', (status, error, message_id, due, self.clock(), row['id']))
                    if error == 'rate_limited':
                        db.execute('INSERT INTO rate_limits(scope,next_at) VALUES(\'bot\',?) ON CONFLICT(scope) DO UPDATE SET next_at=max(next_at,excluded.next_at)', (due,))
            return {'status': 'ok', 'attempted': attempted}

    def resolve_unknown(self, outbox_id, resolution, message_id=None):
        if resolution not in ('accepted', 'cancel', 'retry'):
            raise CoreError('invalid_resolution')
        if resolution == 'accepted' and (not _integer(message_id) or message_id <= 0):
            raise CoreError('message_id_required')
        with self._lock('sender') as acquired:
            if not acquired:
                raise CoreError('sender_busy')
            with self._tx() as db:
                self._recover_sends(db)
                row = db.execute('SELECT * FROM outbox WHERE id=?', (outbox_id,)).fetchone()
                if row is None or row['status'] != 'unknown':
                    raise CoreError('not_unknown')
                if resolution == 'retry':
                    self._binding(db, row['client_id'], usable=True)
                    if row['recipient']:
                        self._binding(db, row['recipient'], usable=True)
                status = {'accepted': 'sent', 'cancel': 'rejected', 'retry': 'queued'}[resolution]
                db.execute('UPDATE outbox SET status=?,error=?,message_id=?,due=?,updated=? WHERE id=?', (status, None if resolution == 'accepted' else 'operator_' + resolution, message_id if resolution == 'accepted' else None, self.clock(), self.clock(), outbox_id))
                result = db.execute('SELECT * FROM outbox WHERE id=?', (outbox_id,)).fetchone()
                return self._outbox_status(result)
