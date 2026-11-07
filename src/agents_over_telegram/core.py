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

    def __init__(self, code: str, *, details: dict | None = None):
        self.code = code
        # Rolling limits set details with numbers only: the limit, its window,
        # current usage and the earliest retry time. owner_chat_not_started
        # adds the registered bot username, which the owner must /start.
        self.details = details
        self.retry_at = (details or {}).get('retry_at')
        super().__init__(code)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _telegram_message_keys(payload):
    """Return (chat_id, message_id) for a captured Telegram message, else None.

    Reaction deliveries are excluded: a reaction is not itself a reaction target.
    """
    if not isinstance(payload, dict) or payload.get('origin') != 'telegram' or payload.get('kind') == 'reaction':
        return None
    chat_id, message_id = payload.get('chat_id'), payload.get('message_id')
    if not _integer(chat_id) or not _integer(message_id):
        return None
    return chat_id, message_id


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _bounded(value: Any, maximum: int = 128) -> str:
    return ''.join(c for c in str(value)[:maximum] if c.isprintable())


_MEDIA_KINDS = ('document', 'photo', 'video', 'audio', 'voice', 'animation')
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024  # The Bot API getFile limit.
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # The Bot API sendPhoto size limit.
MAX_IMAGE_CAPTION = 1024  # The Bot API caption length limit, label header included.

def _image_type(data):
    """Classify image bytes as (send_as, extension, mime), or (None, None, None).

    PNG, JPEG and WebP are publishable as photos. Telegram does not take a GIF
    as a photo, so a GIF is published as a document.
    """
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'photo', 'png', 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'photo', 'jpg', 'image/jpeg'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'photo', 'webp', 'image/webp'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'document', 'gif', 'image/gif'
    return None, None, None


def _attachment(kind, media):
    """Keep only bounded file metadata; Telegram's file_id is what fetching needs."""
    file_id, unique = media.get('file_id'), media.get('file_unique_id')
    if not isinstance(file_id, str) or not 1 <= len(file_id) <= 256:
        return None
    if not isinstance(unique, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', unique):
        return None
    item = {'kind': kind, 'file_id': file_id, 'file_unique_id': unique}
    if isinstance(media.get('file_name'), str):
        item['file_name'] = _bounded(media['file_name'], 255)
    if isinstance(media.get('mime_type'), str):
        item['mime_type'] = _bounded(media['mime_type'], 128)
    if _integer(media.get('file_size')) and media['file_size'] >= 0:
        item['file_size'] = media['file_size']
    return item


def _message_content(msg):
    """Return (text, attachments), or (None, []) for an unsupported message.

    A media message carries its caption as text, or an empty string."""
    attachments = []
    for kind in _MEDIA_KINDS:
        media = msg.get(kind)
        if kind == 'photo':
            # Telegram sends one entry per size; keep the largest.
            sizes = [size for size in media if isinstance(size, dict)] if isinstance(media, list) else []
            media = max(sizes, key=lambda size: size.get('file_size') if _integer(size.get('file_size')) else 0,
                        default=None)
        if isinstance(media, dict):
            item = _attachment(kind, media)
            if item is not None:
                attachments.append(item)
    text = msg.get('text') if isinstance(msg.get('text'), str) else msg.get('caption')
    if not isinstance(text, str):
        text = '' if attachments else None
    if text is None or len(text) > 4096:
        return None, []
    return text, attachments


def _addressed(msg, text, account):
    """True when the message mentions the shared bot, replies to it, or addresses a command to it."""
    username = re.escape(account['username'])
    if re.search(r'(?<![\w@])@' + username + r'(?!\w)', text, re.IGNORECASE):
        return True
    if re.search(r'(?<![\w/])/[A-Za-z0-9_]+@' + username + r'(?!\w)', text, re.IGNORECASE):
        return True
    replied = _mapping(_mapping(msg.get('reply_to_message')).get('from'))
    return replied.get('id') == account['bot_id']


def _forward_origin(msg):
    """Who a forwarded message came from, bounded, or None for an ordinary message."""
    origin = _mapping(msg.get('forward_origin'))
    kind = origin.get('type')
    if kind == 'user':
        user = _mapping(origin.get('sender_user'))
        name = user.get('username') or ' '.join(
            part for part in (user.get('first_name'), user.get('last_name')) if isinstance(part, str))
        found = {'type': 'user', 'id': user['id'] if _integer(user.get('id')) else None}
    elif kind == 'hidden_user':
        name, found = origin.get('sender_user_name'), {'type': 'hidden_user'}
    elif kind in ('chat', 'channel'):
        chat = _mapping(origin.get('sender_chat') if kind == 'chat' else origin.get('chat'))
        name = chat.get('title') or chat.get('username')
        found = {'type': kind, 'id': chat['id'] if _integer(chat.get('id')) else None}
    else:
        return None
    found['name'] = _bounded(name, 128) if isinstance(name, str) else ''
    if _integer(origin.get('date')):
        found['date'] = origin['date']
    return found


QUOTED_TEXT_LIMIT = 1024


def _quoted_message(msg, accept_media, allowed_sender):
    """The message a reply quotes, so an agent can read what was replied to.

    Telegram includes the replied-to message in the update. Its text and media
    are kept like a delivered message's: media only when the connection accepts
    it, otherwise only the media kinds. None when nothing readable is quoted, for
    example a forum topic's creation message, which Telegram sets as the replied
    message of every message in that topic. The text is cut to
    QUOTED_TEXT_LIMIT characters, so a full poll page stays under the result
    cap. ``allowed_sender`` tells whether the quoted sender is on the allow
    list: an allowed sender quoting someone else passes that content on, like
    a forward.
    """
    quoted = _mapping(msg.get('reply_to_message'))
    if not _integer(quoted.get('message_id')):
        return None
    text, attachments = _message_content(quoted)
    if text is None:
        return None
    sender = _mapping(quoted.get('from'))
    sender_id = sender['id'] if _integer(sender.get('id')) else None
    is_bot = sender.get('is_bot') is True
    item = {'message_id': quoted['message_id'], 'text': text[:QUOTED_TEXT_LIMIT],
            'sender': {'id': sender_id, 'is_bot': is_bot,
                       'username': _bounded(sender.get('username', ''), 64)
                       if isinstance(sender.get('username'), str) else '',
                       'allowed': bool(sender_id is not None and allowed_sender(sender_id, is_bot))}}
    if len(text) > QUOTED_TEXT_LIMIT:
        item['truncated'] = True
    if attachments and accept_media:
        item['attachments'] = attachments
    elif attachments:
        item['media_refused'] = sorted({a['kind'] for a in attachments})
    return item


def reaction_emoji(value):
    """Normalize one agent-provided emoji to Telegram's documented form.

    Telegram's bot reaction list uses bare emoji without variation selectors,
    so they are stripped before the exact allowlist comparison. None or '' means
    "remove the bot's reaction". Anything else that is not allowlisted raises.
    """
    if value is None:
        return ''
    if not isinstance(value, str) or len(value) > 32:
        raise CoreError('invalid_reaction_emoji')
    normalized = value.replace('\ufe0f', '')
    if value and not normalized:
        # Input that was only variation selectors is not a removal request.
        raise CoreError('invalid_reaction_emoji')
    if normalized and normalized not in Core.REACTION_EMOJI:
        raise CoreError('invalid_reaction_emoji')
    return normalized


def reaction_types(value):
    """Map a Telegram reaction list to bounded markers, or None when malformed.

    Emoji reactions keep their emoji. Custom-emoji and paid reactions, which a
    bot cannot send, are summarized as fixed markers without opaque ids.
    """
    if not isinstance(value, list) or len(value) > 64:
        return None
    mapped = []
    for item in value:
        kind = _mapping(item).get('type')
        if kind == 'emoji':
            emoji = _mapping(item).get('emoji')
            if not isinstance(emoji, str) or not 1 <= len(emoji) <= 32:
                return None
            mapped.append(emoji)
        elif kind == 'custom_emoji':
            mapped.append('custom_emoji')
        elif kind == 'paid':
            mapped.append('paid')
        else:
            return None
    return mapped[:Core.REACTION_LIST_MAX], len(mapped) > Core.REACTION_LIST_MAX


class Core:
    PAIR_TTL = 600
    # Defaults for the rolling send limits. The operator can change them for
    # the whole state directory with exchange_limits().
    CONVERSATION_SEND_LIMIT = 30
    CONVERSATION_WINDOW = 3600
    # A ceiling per agent across all its conversations, so the total does not
    # grow with the number of conversations an agent or bot can reach.
    CLIENT_SEND_LIMIT = 60
    CLIENT_WINDOW = 3600
    BOT_PAIR_SEND_LIMIT = 32
    BOT_PAIR_WINDOW = 3600
    SEND_LIMIT_RANGE = (1, 1000)
    WINDOW_RANGE = (60, 86400)
    UNMATCHED_LIMIT = 100
    BOT_INTERVAL = 0.05
    DESTINATION_INTERVAL = 1.0
    CONVERSATION_INTERVAL = 1.0
    HISTORY_WINDOW = 86400
    HISTORY_CURSOR_TTL = 900
    # Reactions never post a message, so they carry their own rolling limit,
    # separate from every message send limit. One reaction per message is the
    # Bot API's bot limit; only standard emoji reactions are sent.
    REACTION_SEND_LIMIT = 30
    REACTION_WINDOW = 3600
    REACTION_INTERVAL = 1.0
    REACTION_LIST_MAX = 16
    # owner_notify interrupts the owner personally, so it stays low volume by
    # rule: (limit, window seconds) pairs, each a rolling window per agent.
    OWNER_NOTIFY_LIMITS = ((3, 3600), (10, 86400))
    OWNER_NOTIFY_INTERVAL = 1.0
    OWNER_NOTIFY_FOOTER = 'Reply in the group, not here.'
    REACTION_WAKE_MODES = ('own_messages', 'all', 'none')
    WAKE_EXEMPT_MODES = ('addressed,owner', 'addressed', 'owner', 'none')
    WAKE_INTERVAL_MAX = 86400

    # The exact standard reaction emoji Telegram documents for bots
    # (core.telegram.org/bots/api#reactiontypeemoji). Telegram compares the bare
    # forms without variation selectors, so input is normalized the same way.
    REACTION_EMOJI = (
        '❤', '👍', '👎', '🔥', '🥰', '👏', '😁', '🤔', '🤯', '😱', '🤬', '😢', '🎉', '🤩', '🤮',
        '💩', '🙏', '👌', '🕊', '🤡', '🥱', '🥴', '😍', '🐳', '❤‍🔥', '🌚', '🌭', '💯', '🤣', '⚡',
        '🍌', '🏆', '💔', '🤨', '😐', '🍓', '🍾', '💋', '🖕', '😈', '😴', '😭', '🤓', '👻', '👨‍💻',
        '👀', '🎃', '🙈', '😇', '😨', '🤝', '✍', '🤗', '🫡', '🎅', '🎄', '☃', '💅', '🤪', '🗿',
        '🆒', '💘', '🙉', '🦄', '😘', '💊', '🙊', '😎', '👾', '🤷‍♂', '🤷', '🤷‍♀', '😡',
    )

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
                    status TEXT NOT NULL, error TEXT, message_id INTEGER, image TEXT,
                    created REAL NOT NULL, updated REAL NOT NULL, due REAL NOT NULL,
                    UNIQUE(client_id,idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS reaction_outbox (
                    id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES clients(id),
                    chat_id INTEGER NOT NULL, thread_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL, emoji TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL,
                    status TEXT NOT NULL, error TEXT,
                    created REAL NOT NULL, updated REAL NOT NULL, due REAL NOT NULL,
                    UNIQUE(client_id,idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS owner_outbox (
                    id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES clients(id),
                    owner_id INTEGER NOT NULL, text TEXT NOT NULL,
                    from_chat_id INTEGER, forward_message_id INTEGER,
                    idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL,
                    status TEXT NOT NULL, error TEXT,
                    message_id INTEGER, forwarded_message_id INTEGER,
                    created REAL NOT NULL, updated REAL NOT NULL, due REAL NOT NULL,
                    UNIQUE(client_id,idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS owner_outbox_window ON owner_outbox(client_id,created);
                CREATE TABLE IF NOT EXISTS rate_limits (
                    scope TEXT PRIMARY KEY, next_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, send_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS conversation_budget_grants (
                    id INTEGER PRIMARY KEY, client_id TEXT NOT NULL REFERENCES clients(id),
                    conversation_id TEXT NOT NULL REFERENCES conversations(id),
                    send_limit INTEGER NOT NULL, created REAL NOT NULL,
                    expires_at REAL NOT NULL, reason TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS exchange_limits (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    conversation_send_limit INTEGER NOT NULL, conversation_window INTEGER NOT NULL,
                    pair_send_limit INTEGER NOT NULL, pair_window INTEGER NOT NULL,
                    updated REAL NOT NULL, reason TEXT NOT NULL,
                    client_send_limit INTEGER, client_window INTEGER
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
            binding_columns = {row['name'] for row in db.execute('PRAGMA table_info(bindings)')}
            if 'suspension_seq' not in binding_columns:
                db.execute('ALTER TABLE bindings ADD COLUMN suspension_seq INTEGER NOT NULL DEFAULT 0')
            # Both default to the original behavior: every allowed message is
            # delivered, and nothing from other senders is retained.
            if 'delivery_filter' not in binding_columns:
                db.execute("ALTER TABLE bindings ADD COLUMN delivery_filter TEXT NOT NULL DEFAULT 'all'")
            if 'capture_context' not in binding_columns:
                db.execute('ALTER TABLE bindings ADD COLUMN capture_context INTEGER NOT NULL DEFAULT 0')
            # Media messages stay unsupported unless the operator accepts them.
            if 'accept_media' not in binding_columns:
                db.execute('ALTER TABLE bindings ADD COLUMN accept_media INTEGER NOT NULL DEFAULT 0')
            # Context rows are stored acknowledged, so no poll, watch or wake path
            # ever claims them; history shows them as context.
            if 'context' not in {row['name'] for row in db.execute('PRAGMA table_info(inbox)')}:
                db.execute('ALTER TABLE inbox ADD COLUMN context INTEGER NOT NULL DEFAULT 0')
            db.execute('CREATE INDEX IF NOT EXISTS inbox_context_expiry ON inbox(COALESCE(event_at,created)) WHERE context=1')
            if 'window_reset_at' not in {row['name'] for row in db.execute('PRAGMA table_info(conversations)')}:
                # Set when the owner posts in a conversation; NULL means never.
                db.execute('ALTER TABLE conversations ADD COLUMN window_reset_at REAL')
            if 'additional_sends' not in {row['name'] for row in db.execute('PRAGMA table_info(conversation_budget_grants)')}:
                # Older grants were lifetime allowances. They do not apply to
                # rolling windows, so they keep a NULL value and are ignored.
                db.execute('ALTER TABLE conversation_budget_grants ADD COLUMN additional_sends INTEGER')
            limit_columns = {row['name'] for row in db.execute('PRAGMA table_info(exchange_limits)')}
            for column in ('client_send_limit', 'client_window'):
                if column not in limit_columns:
                    # NULL means the built-in default.
                    db.execute(f'ALTER TABLE exchange_limits ADD COLUMN {column} INTEGER')
            # An image send stores a copy of the bytes for retries; NULL is text-only.
            if 'image' not in {row['name'] for row in db.execute('PRAGMA table_info(outbox)')}:
                db.execute('ALTER TABLE outbox ADD COLUMN image TEXT')
            # Reaction receive capability: NULL until first observed or probed.
            if 'bot_admin' not in binding_columns:
                db.execute('ALTER TABLE bindings ADD COLUMN bot_admin INTEGER')
            # Which reactions are delivered (and can wake): the default wakes only
            # for reactions on the agent's own published messages.
            if 'reaction_wake' not in binding_columns:
                db.execute("ALTER TABLE bindings ADD COLUMN reaction_wake TEXT NOT NULL DEFAULT 'own_messages'")
            # Wake batching: at most one notice per interval (0 = every delivery
            # at once), except for the kinds of delivery named in wake_exempt.
            if 'wake_interval' not in binding_columns:
                db.execute('ALTER TABLE bindings ADD COLUMN wake_interval INTEGER NOT NULL DEFAULT 0')
            if 'wake_exempt' not in binding_columns:
                db.execute("ALTER TABLE bindings ADD COLUMN wake_exempt TEXT NOT NULL DEFAULT 'addressed,owner'")
            # owner_notify is the first tool that writes outside the bound
            # destination, so the owner switches it on knowingly per connection.
            if 'owner_notify' not in binding_columns:
                db.execute('ALTER TABLE bindings ADD COLUMN owner_notify INTEGER NOT NULL DEFAULT 0')
            # When the last admin probe ran, so a failing probe is retried only after a pause.
            if 'bot_admin_probed' not in binding_columns:
                db.execute('ALTER TABLE bindings ADD COLUMN bot_admin_probed REAL')
            # Telegram chat and message IDs of captured messages, so a reaction finds
            # its target with an index lookup instead of decoding the whole inbox.
            inbox_columns = {row['name'] for row in db.execute('PRAGMA table_info(inbox)')}
            if 'tg_message_id' not in inbox_columns:
                db.execute('ALTER TABLE inbox ADD COLUMN tg_chat_id INTEGER')
                db.execute('ALTER TABLE inbox ADD COLUMN tg_message_id INTEGER')
                for row in db.execute('SELECT rowid,payload FROM inbox').fetchall():
                    keys = _telegram_message_keys(json.loads(row['payload']))
                    if keys is not None:
                        db.execute('UPDATE inbox SET tg_chat_id=?,tg_message_id=? WHERE rowid=?', (*keys, row['rowid']))
            db.execute('CREATE INDEX IF NOT EXISTS inbox_telegram_message ON inbox(tg_chat_id,tg_message_id)')
            db.execute('CREATE INDEX IF NOT EXISTS reaction_outbox_history ON reaction_outbox(client_id,created DESC)')
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
        except sqlite3.Error as error:
            code = getattr(error, 'sqlite_errorcode', None)
            if isinstance(code, int) and code & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise CoreError('storage_busy') from None
            raise CoreError('storage_error') from None
        finally:
            if db is not None:
                db.close()

    @contextlib.contextmanager
    def _tx(self):
        with self._db() as db:
            try:
                db.execute('BEGIN IMMEDIATE')
            except sqlite3.OperationalError as error:
                code = getattr(error, 'sqlite_errorcode', None)
                if not isinstance(code, int) or code & 0xff != sqlite3.SQLITE_BUSY:
                    raise
                # No transaction started. Retry acquisition once without replaying work.
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
            # New connections accept media: a refused photo is easy to miss.
            db.execute('INSERT INTO bindings(client_id,chat_id,thread_id,owner_id,title,created,accept_media) VALUES(?,?,?,?,?,?,1)', (row['client_id'], row['chat_id'], row['thread_id'], row['sender_id'], row['title'], now))
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
                # Backfill only on enablement. Disable leaves memberships already
                # created, and never starts enforcing a pair that has not opted in.
                # Never alter an existing conversation, inbox, outbox or chain counter.
                if enabled:
                    db.execute('INSERT OR IGNORE INTO bot_exchange_members VALUES(?,?,?)',
                               (f'bot:{client_id}:{sender_id}', client_id, sender_id))
                    for admitted in db.execute('SELECT conversation_id,payload FROM inbox WHERE client_id=? AND context=0', (client_id,)).fetchall():
                        payload = json.loads(admitted['payload'])
                        sender = _mapping(payload.get('sender'))
                        if payload.get('origin') == 'telegram' and sender.get('id') == sender_id and sender.get('is_bot') is True:
                            db.execute('INSERT OR IGNORE INTO bot_exchange_members VALUES(?,?,?)',
                                       (admitted['conversation_id'], client_id, sender_id))
            policy = db.execute('SELECT enabled FROM bot_exchange_policies WHERE client_id=? AND sender_id=?',
                                (client_id, sender_id)).fetchone()
            limits = self._limits(db)
            usage = self._bot_pair_usage(db, client_id, sender_id, self.clock(), limits)
        return {'sender_id': sender_id, 'enabled': bool(policy and policy[0]),
                'max_sends_per_exchange': limits['conversation_send_limit'],
                'conversation_send_limit': limits['conversation_send_limit'],
                'conversation_window_seconds': limits['conversation_window_seconds'],
                'pair_send_limit': limits['pair_send_limit'],
                'window_seconds': limits['pair_window_seconds'], 'reserved_in_window': usage[0],
                'retry_at': usage[1]}

    DELIVERY_FILTERS = ('all', 'addressed')

    def delivery_settings(self, client_id, delivery_filter=None, capture_context=None, accept_media=None,
                          reaction_wake=None, wake_interval=None, wake_exempt=None, owner_notify=None):
        """Local operator control; never exposed as an agent MCP mutation.

        ``delivery_filter`` 'all' (the default) delivers every message from an
        allowed sender. 'addressed' delivers only messages that mention the bot,
        reply to it or carry a command addressed to it; the rest are kept as
        context. ``capture_context`` also keeps messages from senders that are not
        allowed, as context only. Context is never delivered, leased or announced.
        ``accept_media`` accepts documents, photos, video, audio, voice notes and
        animations, with their caption as text; otherwise they stay unsupported.
        ``reaction_wake`` decides which incoming reactions from allowed senders
        are delivered and can wake this agent: 'own_messages' (the default) only
        reactions on messages this agent published, 'all' every reaction, 'none'
        none; the rest are kept as context. The delivery filter does not apply to
        reactions, which target one message already. A new connection accepts
        media; connections made before that default keep their setting. With
        media off, a media message from an allowed sender is still delivered,
        with its caption and a ``media_refused`` list of kinds instead of the
        attachments. ``wake_interval`` batches notices: after one, the next waits
        this many seconds and then covers everything that arrived meanwhile; 0
        (the default) announces each delivery at once. Deliveries still queue
        immediately and can always be polled. ``wake_exempt`` names what is
        announced at once anyway: 'addressed,owner' (the default) for messages
        that address the bot and messages from the owner, 'addressed', 'owner'
        or 'none'. ``owner_notify`` lets the agent send private notices to
        the owner with the owner_notify tool; it is off until the owner turns
        it on. The other settings default to the original behavior.
        """
        if delivery_filter is not None and delivery_filter not in self.DELIVERY_FILTERS:
            raise CoreError('invalid_delivery_settings')
        if any(value is not None and type(value) is not bool for value in (capture_context, accept_media, owner_notify)):
            raise CoreError('invalid_delivery_settings')
        if reaction_wake is not None and reaction_wake not in self.REACTION_WAKE_MODES:
            raise CoreError('invalid_delivery_settings')
        if wake_interval is not None and (not _integer(wake_interval)
                                          or not 0 <= wake_interval <= self.WAKE_INTERVAL_MAX):
            raise CoreError('invalid_delivery_settings')
        if wake_exempt is not None and wake_exempt not in self.WAKE_EXEMPT_MODES:
            raise CoreError('invalid_delivery_settings')
        with self._tx() as db:
            self._binding(db, client_id)
            if delivery_filter is not None:
                db.execute('UPDATE bindings SET delivery_filter=? WHERE client_id=? AND connected=1', (delivery_filter, client_id))
            if capture_context is not None:
                db.execute('UPDATE bindings SET capture_context=? WHERE client_id=? AND connected=1', (int(capture_context), client_id))
            if accept_media is not None:
                db.execute('UPDATE bindings SET accept_media=? WHERE client_id=? AND connected=1', (int(accept_media), client_id))
            if reaction_wake is not None:
                db.execute('UPDATE bindings SET reaction_wake=? WHERE client_id=? AND connected=1', (reaction_wake, client_id))
            if wake_interval is not None:
                db.execute('UPDATE bindings SET wake_interval=? WHERE client_id=? AND connected=1', (wake_interval, client_id))
            if wake_exempt is not None:
                db.execute('UPDATE bindings SET wake_exempt=? WHERE client_id=? AND connected=1', (wake_exempt, client_id))
            if owner_notify is not None:
                db.execute('UPDATE bindings SET owner_notify=? WHERE client_id=? AND connected=1', (int(owner_notify), client_id))
            row = self._binding(db, client_id)
        return {'delivery_filter': row['delivery_filter'], 'capture_context': bool(row['capture_context']),
                'accept_media': bool(row['accept_media']), 'reaction_wake': row['reaction_wake'],
                'wake_interval': row['wake_interval'], 'wake_exempt': row['wake_exempt'],
                'owner_notify': bool(row['owner_notify']),
                'context_window_seconds': self.HISTORY_WINDOW}

    def _limits(self, db):
        row = db.execute('SELECT * FROM exchange_limits WHERE singleton=1').fetchone()
        if row is None:
            return {'conversation_send_limit': self.CONVERSATION_SEND_LIMIT,
                    'conversation_window_seconds': self.CONVERSATION_WINDOW,
                    'client_send_limit': self.CLIENT_SEND_LIMIT,
                    'client_window_seconds': self.CLIENT_WINDOW,
                    'pair_send_limit': self.BOT_PAIR_SEND_LIMIT,
                    'pair_window_seconds': self.BOT_PAIR_WINDOW, 'source': 'default'}
        return {'conversation_send_limit': row['conversation_send_limit'],
                'conversation_window_seconds': row['conversation_window'],
                'client_send_limit': row['client_send_limit'] or self.CLIENT_SEND_LIMIT,
                'client_window_seconds': row['client_window'] or self.CLIENT_WINDOW,
                'pair_send_limit': row['pair_send_limit'],
                'pair_window_seconds': row['pair_window'], 'source': 'operator',
                'updated_at': row['updated'], 'reason': row['reason']}

    def exchange_limits(self, *, conversation_send_limit=None, conversation_window_seconds=None,
                        client_send_limit=None, client_window_seconds=None,
                        pair_send_limit=None, pair_window_seconds=None, reset=False, reason=None):
        """Inspect or change the rolling send limits through the local operator CLI.

        The values live in the shared state, so every agent process using this
        bot applies the same limits. No MCP tool can change them.
        """
        values = {'conversation_send_limit': (conversation_send_limit, self.SEND_LIMIT_RANGE),
                  'conversation_window_seconds': (conversation_window_seconds, self.WINDOW_RANGE),
                  'client_send_limit': (client_send_limit, self.SEND_LIMIT_RANGE),
                  'client_window_seconds': (client_window_seconds, self.WINDOW_RANGE),
                  'pair_send_limit': (pair_send_limit, self.SEND_LIMIT_RANGE),
                  'pair_window_seconds': (pair_window_seconds, self.WINDOW_RANGE)}
        changing = reset or any(value is not None for value, _ in values.values())
        for value, (low, high) in values.values():
            if value is not None and (not _integer(value) or not low <= value <= high):
                raise CoreError('invalid_exchange_limits')
        if reset and any(value is not None for value, _ in values.values()):
            raise CoreError('invalid_exchange_limits')
        if changing and (not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500):
            raise CoreError('invalid_exchange_limits')
        if not changing and reason is not None:
            raise CoreError('invalid_exchange_limits')
        with self._tx() as db:
            if reset:
                db.execute('DELETE FROM exchange_limits')
            elif changing:
                current = self._limits(db)
                merged = {key: value if value is not None else current[key]
                          for key, (value, _) in values.items()}
                db.execute('INSERT INTO exchange_limits(singleton,conversation_send_limit,conversation_window,'
                           'pair_send_limit,pair_window,updated,reason,client_send_limit,client_window) '
                           'VALUES(1,?,?,?,?,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET '
                           'conversation_send_limit=excluded.conversation_send_limit,'
                           'conversation_window=excluded.conversation_window,'
                           'pair_send_limit=excluded.pair_send_limit,pair_window=excluded.pair_window,'
                           'updated=excluded.updated,reason=excluded.reason,'
                           'client_send_limit=excluded.client_send_limit,client_window=excluded.client_window',
                           (merged['conversation_send_limit'], merged['conversation_window_seconds'],
                            merged['pair_send_limit'], merged['pair_window_seconds'],
                            self.clock(), reason.strip(),
                            merged['client_send_limit'], merged['client_window_seconds']))
            return self._limits(db)

    @staticmethod
    def _window_usage(stamps, limit, window):
        """Count reservations in a rolling window, newest first.

        Returns the count and, when the window is full, the time the newest
        limit-th reservation expires so that one more send fits.
        """
        return len(stamps), (stamps[limit - 1] + window if len(stamps) >= limit else None)

    def _bot_pair_usage(self, db, client_id, sender_id, now, limits):
        window = limits['pair_window_seconds']
        stamps = [r[0] for r in db.execute('''SELECT o.created FROM bot_exchange_members p
            JOIN outbox o ON o.conversation_id=p.conversation_id
            WHERE p.client_id=? AND p.sender_id=? AND o.created>?
            ORDER BY o.created DESC''',
            (client_id, sender_id, now - window))]
        return self._window_usage(stamps, limits['pair_send_limit'], window)

    def _conversation_usage(self, db, conversation_id, now, limit, limits):
        window = limits['conversation_window_seconds']
        # A message from the owner in this conversation starts its window again:
        # sends before it no longer count toward this conversation's limit.
        # A send at the same instant as the owner's message still counts.
        row = db.execute('SELECT window_reset_at FROM conversations WHERE id=?', (conversation_id,)).fetchone()
        reset_at = row[0] if row and row[0] is not None else None
        stamps = [r[0] for r in db.execute(
            'SELECT created FROM outbox WHERE conversation_id=? AND created>? AND created>=? ORDER BY created DESC',
            (conversation_id, now - window, reset_at if reset_at is not None else now - window))]
        return self._window_usage(stamps, limit, window)

    def _client_usage(self, db, client_id, now, limits):
        window = limits['client_window_seconds']
        stamps = [r[0] for r in db.execute(
            'SELECT created FROM outbox WHERE client_id=? AND created>? ORDER BY created DESC',
            (client_id, now - window))]
        return self._window_usage(stamps, limits['client_send_limit'], window)

    @staticmethod
    def _limit_error(code, used, limit, window, retry_at, now):
        return CoreError(code, details={
            'sent_in_window': used, 'limit': limit, 'window_seconds': window,
            'retry_at': retry_at, 'retry_after_seconds': max(0, math.ceil(retry_at - now))})

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
            from .relay import WAIT_FOR_DESKTOP_WAKE, unresolved_wakes
            held = unresolved_wakes(db, now, client_id)
            held_wake = {key: held[0][key] for key in ('notification_id', 'status', 'since', 'age_seconds',
                                                       'attempt', 'handoff_phase', 'wake_attempts',
                                                       'next_action')} if held else None
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
                if row['suspended'].startswith('group_migrated:'):
                    message = ('This group moved to a new chat id. Queued messages are kept. Ask the local owner to '
                               'run "aot unsuspend" on this computer to move the connection to the new chat.')
                elif row['suspended'] == 'topic_changed':
                    message = ('This topic was closed or changed. Queued messages are kept. After the topic is open, '
                               'ask the local owner to run "aot unsuspend --topic-open" on this computer.')
                else:
                    message = ('The bot was removed or restricted in this group. Queued messages are kept. After the bot '
                               'can post there again, ask the local owner to run "aot unsuspend" on this computer.')
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
                elif held_wake and held_wake['next_action'] == WAIT_FOR_DESKTOP_WAKE:
                    action = WAIT_FOR_DESKTOP_WAKE
                    message = ('A wake for this agent is queued in the desktop app but has not opened yet, so no new '
                               'wakes are sent. A running listener retries it while the desktop is open and this '
                               'connection is active; nobody needs to resolve it. If no listener is running, ask the '
                               'local owner to start one. Pending messages can still be polled.')
                elif held_wake:
                    action = held_wake['next_action']
                    message = ('An automatic wake for this agent has been unresolved for a long time, so no new '
                               'wakes are sent. Ask the local owner to review it. Pending messages can still be polled.')
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
                'unresolved_wake': held_wake,
                'history': {'window_hours': self.HISTORY_WINDOW // 3600, 'kind': 'captured_only'},
                'routing': {'delivery_filter': row['delivery_filter'],
                            'capture_context': bool(row['capture_context']),
                            'accept_media': bool(row['accept_media']),
                            'wake_interval': row['wake_interval'],
                            'wake_exempt': row['wake_exempt'],
                            'owner_notify': bool(row['owner_notify'])} if row else None,
                'reactions': self._reaction_capability(row) if row else None,
                'state': state, 'next_action': {'code': action, 'message': message},
            }

    @staticmethod
    def _reaction_capability(row):
        """Read-only reaction facts: only local, already-recorded state."""
        if row['bot_admin'] is None:
            receive = {'available': None, 'bot_admin': None, 'requires_bot_admin': True,
                       'note': "The bot's administrator status in this destination is not known yet; the next collection checks it."}
        elif row['bot_admin']:
            receive = {'available': True, 'bot_admin': True, 'requires_bot_admin': True,
                       'note': 'Reaction updates arrive while the shared bot stays an administrator in this destination.'}
        else:
            receive = {'available': False, 'bot_admin': False, 'requires_bot_admin': True,
                       'note': 'The shared bot is not an administrator in this destination, so reactions from people never arrive. Ask the local owner to promote it to administrator. Collection checks this again about once an hour.'}
        return {'send': {'per_message': 1,
                         'emoji': "one standard reaction emoji Telegram allows for bots, or an empty emoji to remove the bot's reaction",
                         'note': 'Bots cannot use paid reactions, and this tool never sends custom emoji reactions.'},
                'receive': receive, 'wake': row['reaction_wake']}

    BOT_ADMIN_RETRY = 600
    # A known flag is checked again this often: a missed my_chat_member update
    # (for example one consumed by an older runtime) must not leave it stale.
    BOT_ADMIN_RECHECK = 3600

    def _unmatched(self, db, update_id, chat_id, reason, now):
        db.execute('INSERT OR IGNORE INTO unmatched(update_id,chat_id,reason,created) VALUES(?,?,?,?)', (update_id, chat_id, reason, now))
        db.execute('DELETE FROM unmatched WHERE update_id NOT IN (SELECT update_id FROM unmatched ORDER BY update_id DESC LIMIT ?)', (self.UNMATCHED_LIMIT,))

    def _enqueue(self, db, client_id, event_key, payload, conversation_id, now, outbox_id=None, event_at=None):
        if db.execute('SELECT 1 FROM inbox WHERE client_id=? AND event_key=?', (client_id, event_key)).fetchone():
            return
        count = db.execute('SELECT count(*) FROM inbox WHERE client_id=? AND acknowledged=0', (client_id,)).fetchone()[0]
        if count >= self.queue_limit:
            raise CoreError('queue_full')
        keys = _telegram_message_keys(payload) or (None, None)
        db.execute('INSERT INTO inbox(id,client_id,event_key,payload,created,conversation_id,outbox_id,event_at,tg_chat_id,tg_message_id) VALUES(?,?,?,?,?,?,?,?,?,?)', ('msg_' + secrets.token_hex(12), client_id, event_key, json.dumps(payload, ensure_ascii=False), now, conversation_id, outbox_id, now if event_at is None else event_at, *keys))

    # A new suspension never replaces a reason that needs more owner work.
    # Clearing topic_changed also checks bot membership, and a migration
    # cannot be cleared at all, so keep the strongest observed reason.
    _SUSPEND_OVER = {
        'bot_removed_or_restricted': "(suspended IS NULL OR suspended='bot_removed_or_restricted')",
        'topic_changed': "(suspended IS NULL OR suspended IN ('bot_removed_or_restricted','topic_changed'))",
        'group_migrated': "(suspended IS NULL OR suspended NOT LIKE 'group_migrated:%')",
    }

    def _suspend(self, db, reason, chat_id, thread_id=None):
        # Every observed event bumps suspension_seq, even with the same reason,
        # so an owner clear that raced with it cannot erase it.
        guard = self._SUSPEND_OVER[reason.split(':')[0]]
        scope = 'chat_id=? AND (? IS NULL OR thread_id=?) AND connected=1'
        # Bindings that keep a stronger reason still get a new seq. Run this
        # first so each binding moves exactly once.
        db.execute(f'UPDATE bindings SET suspension_seq=suspension_seq+1 WHERE {scope} AND suspended IS NOT NULL AND NOT {guard}',
                   (chat_id, thread_id, thread_id))
        db.execute(f'UPDATE bindings SET suspended=?,suspension_seq=suspension_seq+1 WHERE {scope} AND {guard}',
                   (reason, chat_id, thread_id, thread_id))

    def _follow_migration(self, db, old_chat_id, new_chat_id):
        """Move the bindings of a group that became a supergroup to its new chat id.

        The client, its allowed senders and its queued work stay the same. A
        binding whose new destination is already taken stays on the old chat
        and is suspended, so the owner must pair it again.
        """
        if old_chat_id == new_chat_id:
            return
        for binding in db.execute('SELECT * FROM bindings WHERE chat_id=? AND connected=1', (old_chat_id,)).fetchall():
            if db.execute('SELECT 1 FROM bindings WHERE chat_id=? AND thread_id=? AND connected=1',
                          (new_chat_id, binding['thread_id'])).fetchone():
                self._suspend(db, 'group_migrated:' + str(new_chat_id), old_chat_id, binding['thread_id'])
                continue
            self._move_binding(db, binding, new_chat_id)

    def _follow_send_migration(self, db, row, new_chat_id):
        """A send was refused because its group became a supergroup.

        This is the third migration signal, after the two service messages. It
        moves the bindings like they do. The refused message is queued again;
        it moves with the binding, or follows a binding that an earlier notice
        already moved. If the binding stays on the old chat, suspended, the
        message waits there like every other queued send.
        """
        self._follow_migration(db, row['chat_id'], new_chat_id)
        db.execute("UPDATE outbox SET chat_id=?,reply_to=NULL WHERE id=? AND status='queued' AND EXISTS("
                   "SELECT 1 FROM bindings WHERE client_id=? AND chat_id=? AND connected=1)",
                   (new_chat_id, row['id'], row['client_id'], new_chat_id))

    def _move_binding(self, db, binding, new_chat_id):
        client_id, old_chat_id = binding['client_id'], binding['chat_id']
        # A suspension for this same migration no longer applies. Any other
        # reason stays. The new seq makes a racing owner clear fail.
        # The bot's administrator status in the new supergroup is not known yet.
        db.execute('UPDATE bindings SET chat_id=?,suspension_seq=suspension_seq+1,'
                   'suspended=CASE WHEN suspended=? THEN NULL ELSE suspended END,'
                   'bot_admin=NULL,bot_admin_probed=NULL '
                   'WHERE client_id=? AND connected=1',
                   (new_chat_id, 'group_migrated:' + str(new_chat_id), client_id))
        # Message ids are per chat: the supergroup numbers its own messages, and
        # the old group's messages stay behind in the closed group. A queued send
        # moves without its reply link, and a queued reaction cannot move at all;
        # in the new chat both would hit an unrelated message with the same id.
        db.execute("UPDATE outbox SET chat_id=?,reply_to=NULL WHERE client_id=? AND chat_id=? AND status='queued'",
                   (new_chat_id, client_id, old_chat_id))
        db.execute("UPDATE reaction_outbox SET status='rejected',error='group_migrated',updated=? "
                   "WHERE client_id=? AND chat_id=? AND status='queued'", (self.clock(), client_id, old_chat_id))

    def _route_update(self, db, update, account, now):
        update_id = update['update_id']
        membership = update.get('my_chat_member')
        if isinstance(membership, dict):
            chat_id = _mapping(membership.get('chat')).get('id')
            new = _mapping(membership.get('new_chat_member'))
            if _integer(chat_id) and _mapping(new.get('user')).get('id') == account['bot_id']:
                # Receiving reactions requires administrator status, so every
                # membership event refreshes what status can tell the owner.
                db.execute('UPDATE bindings SET bot_admin=?,bot_admin_probed=? WHERE chat_id=? AND connected=1',
                           (1 if new.get('status') == 'administrator' else 0, now, chat_id))
                if (new.get('status') in ('left', 'kicked') or (new.get('status') == 'restricted' and (not new.get('is_member', True) or not new.get('can_send_messages', True)))):
                    self._suspend(db, 'bot_removed_or_restricted', chat_id)
            return
        reaction = update.get('message_reaction')
        if isinstance(reaction, dict):
            self._route_reaction(db, update_id, reaction, account, now)
            return
        kind = 'edited_message' if 'edited_message' in update else 'message'
        msg = update.get(kind)
        if not isinstance(msg, dict):
            self._unmatched(db, update_id, None, 'unsupported_update', now)
            return
        chat = _mapping(msg.get('chat'))
        chat_id = chat.get('id')
        # Telegram also sets message_thread_id on replies in a supergroup that is
        # not a forum: it is the first message of the reply chain, not a topic.
        # Only a forum has topics, so only a forum's thread id selects a binding.
        thread_id = msg.get('message_thread_id', 0) if chat.get('is_forum') is True else 0
        if chat.get('type') == 'private':
            # Several agents can share the bot, so a private message, including
            # the owner's /start and replies to owner_notify, has no single
            # agent to go to. Its own reason keeps it from looking like a bug.
            self._unmatched(db, update_id, None, 'private_chat', now)
            return
        if not _integer(chat_id) or not _integer(thread_id) or thread_id < 0 or chat.get('type') not in ('group', 'supergroup'):
            self._unmatched(db, update_id, None, 'unsupported_destination', now)
            return
        # Telegram announces a group's upgrade to a supergroup twice: in the old
        # group (migrate_to_chat_id) and in the new supergroup
        # (migrate_from_chat_id). Either one moves the bindings.
        if _integer(msg.get('migrate_to_chat_id')):
            self._follow_migration(db, chat_id, msg['migrate_to_chat_id'])
            return
        if _integer(msg.get('migrate_from_chat_id')):
            self._follow_migration(db, msg['migrate_from_chat_id'], chat_id)
            return
        if 'forum_topic_closed' in msg or 'forum_topic_edited' in msg:
            self._suspend(db, 'topic_changed', chat_id, thread_id)
            return
        sender = _mapping(msg.get('from'))
        sender_id = sender.get('id')
        if not _integer(sender_id) or sender_id <= 0 or not isinstance(sender.get('is_bot'), bool) or msg.get('sender_chat'):
            self._unmatched(db, update_id, chat_id, 'unverified_sender', now)
            return
        if sender_id == account['bot_id']:
            self._unmatched(db, update_id, chat_id, 'shared_bot_echo', now)
            return
        text, attachments = _message_content(msg)
        if text is None or not _integer(msg.get('message_id')):
            self._unmatched(db, update_id, chat_id, 'unsupported_message', now)
            return
        # Commands are only ever read from message text, never from a caption.
        command_text = msg['text'] if isinstance(msg.get('text'), str) else ''
        command = re.fullmatch(r'/pair@([A-Za-z0-9_]+) ([A-F0-9]{12})', command_text)
        if command and command.group(1).lower() == account['username'].lower():
            if not sender['is_bot'] and kind == 'message':
                db.execute('UPDATE pairings SET sender_id=?,chat_id=?,thread_id=?,title=?,observed=? WHERE code_hash=? AND consumed=0 AND expires>? AND sender_id IS NULL', (sender_id, chat_id, thread_id, _bounded(chat.get('title', ''), 128), now, _digest(command.group(2)), now))
            return
        binding = db.execute('SELECT b.* FROM bindings b JOIN clients c ON c.id=b.client_id WHERE b.chat_id=? AND b.thread_id=? AND b.connected=1 AND c.active=1', (chat_id, thread_id)).fetchone()
        if binding is None:
            self._unmatched(db, update_id, chat_id, 'unbound_destination', now)
            return
        allowed = db.execute('SELECT 1 FROM allowed_senders WHERE client_id=? AND sender_id=? AND is_bot=?', (binding['client_id'], sender_id, int(sender['is_bot']))).fetchone()
        media_refused = []
        if attachments and not binding['accept_media']:
            if not allowed:
                self._unmatched(db, update_id, chat_id, 'unsupported_message', now)
                return
            # Tell the agent that a message came in with media it cannot read,
            # instead of dropping it unseen. Only the media kinds are kept.
            media_refused = sorted({item['kind'] for item in attachments})
            attachments = []
        if not allowed and not binding['capture_context']:
            self._unmatched(db, update_id, chat_id, 'sender_not_allowed', now)
            return
        if allowed and not sender['is_bot'] and sender_id == binding['owner_id'] and command_text.lower() == f'/pause@{account["username"]}'.lower() and kind == 'message':
            db.execute('UPDATE bindings SET paused=1 WHERE client_id=?', (binding['client_id'],))
            return
        # A human message starts its own conversation unless it replies to a known
        # outbound message. Bot replies inherit a known conversation; unsolicited
        # bot messages keep the legacy chain unless the operator enables bounded
        # roots. Every conversation has its own rolling send window, and the
        # per-agent ceiling bounds the total across all of them, so reusing old
        # conversations or minting new ones cannot raise an agent's throughput.
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
        edited_delivery = False
        if kind == 'edited_message':
            # The index holds only captured messages, never reactions, so a
            # reaction on the same message ID cannot pose as the original.
            for original in db.execute('SELECT payload,conversation_id,context FROM inbox '
                                       'WHERE client_id=? AND tg_chat_id=? AND tg_message_id=? ORDER BY created,rowid',
                                       (binding['client_id'], chat_id, msg['message_id'])):
                prior = json.loads(original['payload'])
                if _mapping(prior.get('sender')).get('id') == sender_id:
                    conversation = original['conversation_id']
                    edited_delivery = not original['context']
                    break
        # Membership is recorded for context too: a later reply to a context
        # message from an opted-in bot must stay inside that pair's send budget.
        if sender['is_bot'] and db.execute('''SELECT 1 FROM bot_exchange_policies
                WHERE client_id=? AND sender_id=? AND enabled=1''',
                (binding['client_id'], sender_id)).fetchone():
            db.execute('INSERT OR IGNORE INTO bot_exchange_members VALUES(?,?,?)',
                       (conversation, binding['client_id'], sender_id))
        payload = {'origin': 'telegram', 'kind': 'edited' if kind == 'edited_message' else 'message', 'update_id': update_id, 'message_id': msg['message_id'], 'chat_id': chat_id, 'thread_id': thread_id or None, 'sender': {'id': sender_id, 'is_bot': sender['is_bot'], 'username': _bounded(sender.get('username', ''), 64)}, 'text': text, 'reply_to': reply_to if _integer(reply_to) else None,
                   # Mentions the bot, replies to it or addresses a command to it;
                   # wake batching announces these at once.
                   'addressed': _addressed(msg, text, account)}
        if attachments:
            payload['attachments'] = attachments
        if media_refused:
            payload['media_refused'] = media_refused
        quoted = _quoted_message(msg, binding['accept_media'], lambda quoted_id, quoted_bot: db.execute(
            'SELECT 1 FROM allowed_senders WHERE client_id=? AND sender_id=? AND is_bot=?',
            (binding['client_id'], quoted_id, int(quoted_bot))).fetchone() is not None)
        if quoted is not None:
            payload['reply_to_message'] = quoted
        forwarded = _forward_origin(msg)
        if forwarded is not None:
            payload['forwarded_from'] = forwarded
        stamp = msg.get('edit_date' if kind == 'edited_message' else 'date')
        event_at = stamp if _integer(stamp) and 0 < stamp <= now else now
        if (allowed and not sender['is_bot'] and sender_id == binding['owner_id'] and kind == 'message'
                and not db.execute('SELECT 1 FROM inbox WHERE client_id=? AND event_key=?',
                                   (binding['client_id'], f'tg:{update_id}')).fetchone()):
            # The owner taking part shows a human is following this exchange, so
            # its conversation window starts again from the message's own time.
            # Edits and reprocessed updates do not count, and the per-agent
            # ceiling and pair budgets are not reset.
            db.execute('INSERT INTO conversations(id,window_reset_at) VALUES(?,?) '
                       'ON CONFLICT(id) DO UPDATE SET window_reset_at=excluded.window_reset_at',
                       (conversation, event_at))
        if not allowed:
            reason = 'sender_not_allowed'
        elif binding['delivery_filter'] == 'addressed' and not edited_delivery and not payload['addressed']:
            reason = 'not_addressed'
        else:
            self._enqueue(db, binding['client_id'], f'tg:{update_id}', payload, conversation, now, event_at=event_at)
            return
        self._keep_context(db, binding['client_id'], f'tg:{update_id}', {**payload, 'context_reason': reason},
                           conversation, now, event_at)

    def _route_reaction(self, db, update_id, reaction, account, now):
        """Deliver one message_reaction update under the same rules as messages.

        Telegram does not send reactions set by bots, and sends them only while
        the bot is an administrator. The reaction update carries no thread id, so
        the reacted message is resolved to a destination through the published
        outbox row or a captured inbox row; an unknown message reaches the single
        binding of a plain group, and stays unassigned everywhere else. Sender
        rules match messages. The owner's reaction_wake setting decides which
        reactions are delivered and can wake this agent: by default only
        reactions on messages this agent published; the rest are kept as context.
        """
        chat = _mapping(reaction.get('chat'))
        chat_id = chat.get('id')
        if not _integer(chat_id) or chat.get('type') not in ('group', 'supergroup'):
            self._unmatched(db, update_id, chat_id if _integer(chat_id) else None, 'unsupported_destination', now)
            return
        message_id = reaction.get('message_id')
        if not _integer(message_id) or message_id <= 0:
            self._unmatched(db, update_id, chat_id, 'unsupported_update', now)
            return
        sender = _mapping(reaction.get('user'))
        sender_id = sender.get('id')
        # Anonymous reactions arrive with actor_chat instead of a user, so no
        # allowed-sender rule can apply; they stay unassigned like anonymous
        # channel posts.
        if not _integer(sender_id) or sender_id <= 0 or not isinstance(sender.get('is_bot'), bool) or reaction.get('actor_chat'):
            self._unmatched(db, update_id, chat_id, 'unverified_sender', now)
            return
        if sender_id == account['bot_id']:
            self._unmatched(db, update_id, chat_id, 'shared_bot_echo', now)
            return
        old = reaction_types(reaction.get('old_reaction'))
        new = reaction_types(reaction.get('new_reaction'))
        if old is None or new is None:
            self._unmatched(db, update_id, chat_id, 'unsupported_update', now)
            return
        conversation = None
        binding = None
        own_message = False
        known_message = False
        published = db.execute('SELECT client_id,thread_id,conversation_id FROM outbox '
                               'WHERE chat_id=? AND message_id=? ORDER BY created DESC LIMIT 1',
                               (chat_id, message_id)).fetchone()
        if published is not None:
            known_message = True
            binding = db.execute('SELECT b.* FROM bindings b JOIN clients c ON c.id=b.client_id '
                                 'WHERE b.chat_id=? AND b.thread_id=? AND b.connected=1 AND c.active=1',
                                 (chat_id, published['thread_id'])).fetchone()
            conversation = published['conversation_id']
            own_message = binding is not None and published['client_id'] == binding['client_id']
        else:
            captured = None
            row = db.execute('SELECT client_id,payload,conversation_id FROM inbox WHERE tg_chat_id=? AND tg_message_id=? '
                             'ORDER BY created DESC,rowid DESC LIMIT 1', (chat_id, message_id)).fetchone()
            if row is not None:
                captured = (row['client_id'], json.loads(row['payload']).get('thread_id') or 0, row['conversation_id'])
            if captured is not None:
                known_message = True
                binding = db.execute('SELECT b.* FROM bindings b JOIN clients c ON c.id=b.client_id '
                                     'WHERE b.chat_id=? AND b.thread_id=? AND b.connected=1 AND c.active=1',
                                     (chat_id, captured[1])).fetchone()
                conversation = captured[2]
        if binding is None and not known_message:
            # Unknown message: only a plain group with exactly one binding can be
            # attributed safely; topic destinations stay unassigned.
            candidates = db.execute('SELECT b.* FROM bindings b JOIN clients c ON c.id=b.client_id '
                                    'WHERE b.chat_id=? AND b.connected=1 AND c.active=1', (chat_id,)).fetchall()
            if len(candidates) == 1 and candidates[0]['thread_id'] == 0:
                binding = candidates[0]
        if binding is None:
            reason = 'unbound_destination' if known_message or not db.execute(
                'SELECT 1 FROM bindings b JOIN clients c ON c.id=b.client_id WHERE b.chat_id=? AND b.connected=1 AND c.active=1',
                (chat_id,)).fetchone() else 'unresolved_reaction'
            self._unmatched(db, update_id, chat_id, reason, now)
            return
        allowed = db.execute('SELECT 1 FROM allowed_senders WHERE client_id=? AND sender_id=? AND is_bot=?',
                             (binding['client_id'], sender_id, int(sender['is_bot']))).fetchone()
        if not allowed and not binding['capture_context']:
            self._unmatched(db, update_id, chat_id, 'sender_not_allowed', now)
            return
        payload = {'origin': 'telegram', 'kind': 'reaction', 'update_id': update_id,
                   'message_id': message_id, 'chat_id': chat_id, 'thread_id': binding['thread_id'] or None,
                   'sender': {'id': sender_id, 'is_bot': sender['is_bot'], 'username': _bounded(sender.get('username', ''), 64)},
                   'old_reaction': old[0], 'new_reaction': new[0], 'own_message': own_message}
        if old[1]:
            payload['old_reaction_truncated'] = True
        if new[1]:
            payload['new_reaction_truncated'] = True
        stamp = reaction.get('date')
        event_at = stamp if _integer(stamp) and 0 < stamp <= now else now
        if conversation is None:
            conversation = 'reactions:' + binding['client_id']
        db.execute('INSERT OR IGNORE INTO conversations(id) VALUES(?)', (conversation,))
        if not allowed:
            reason = 'sender_not_allowed'
        elif binding['reaction_wake'] == 'none':
            reason = 'reaction_wake_none'
        elif binding['reaction_wake'] == 'own_messages' and not own_message:
            reason = 'not_own_message'
        else:
            self._enqueue(db, binding['client_id'], 'react:' + str(update_id), payload, conversation, now, event_at=event_at)
            return
        self._keep_context(db, binding['client_id'], 'react:' + str(update_id),
                           {**payload, 'context_reason': reason}, conversation, now, event_at)

    def _keep_context(self, db, client_id, event_key, payload, conversation_id, now, event_at):
        # Stored acknowledged and outside the queue bound: history reads it, no
        # poll, watch, relay or push path ever claims or announces it.
        keys = _telegram_message_keys(payload) or (None, None)
        db.execute('''INSERT OR IGNORE INTO inbox(id,client_id,event_key,payload,created,conversation_id,event_at,acknowledged,context,tg_chat_id,tg_message_id)
            VALUES(?,?,?,?,?,?,?,1,1,?,?)''', ('msg_' + secrets.token_hex(12), client_id, event_key,
                                             json.dumps(payload, ensure_ascii=False), now, conversation_id, event_at, *keys))

    def _prune_context(self, db, now):
        """Drop context older than the history window, with its bookkeeping rows and fetched files.

        The highest inbox rowid is never deleted, so SQLite cannot hand it to a new
        row inside a history snapshot that was frozen at that rowid.
        """
        stale = [row[0] for row in db.execute('''SELECT id FROM inbox WHERE context=1 AND COALESCE(event_at,created)<?
            AND rowid<(SELECT MAX(rowid) FROM inbox)''', (now - self.HISTORY_WINDOW,))]
        if not stale:
            return
        self._delete_attachment_files(stale)
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for start in range(0, len(stale), 500):
            chunk = stale[start:start + 500]
            marks = ','.join('?' * len(chunk))
            for table in ('push_announcements', 'relay_deliveries'):
                if table in tables:
                    db.execute(f'DELETE FROM {table} WHERE inbox_id IN ({marks})', chunk)
            db.execute(f'DELETE FROM inbox WHERE context=1 AND id IN ({marks})', chunk)

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
                    self._prune_context(db, self.clock())
                    db.execute('UPDATE account SET cursor=?,last_gap_at=CASE WHEN last_intake IS NOT NULL AND ?-last_intake>=86400 THEN ? ELSE last_gap_at END,last_intake=?,last_attempt=?,intake_error=NULL WHERE singleton=1', (cursor, self.clock(), self.clock(), self.clock(), self.clock()))
                self._probe_bot_admin(account)
                return {'status': 'ok', 'updates': accepted, 'cursor': cursor}
            except (TransportError, CoreError) as error:
                code = error.code if isinstance(error, CoreError) else 'telegram_intake_failed'
                with self._tx() as db:
                    db.execute('UPDATE account SET last_attempt=?,intake_error=? WHERE singleton=1', (self.clock(), code))
                raise CoreError(code) from None

    def _probe_bot_admin(self, account):
        """Keep bot_admin flags current with getChatMember; never fails collection.

        Reaction updates arrive only while the bot is an administrator. A binding
        with an unknown flag is checked first, and a known flag is checked again
        BOT_ADMIN_RECHECK seconds after it was last set, because the
        my_chat_member update that would change it can be missed: an older
        runtime on the same bot may consume it, or the promotion may predate the
        pairing. A definite client error means the bot cannot even see the chat,
        which is recorded as not-admin; other failures keep the flag as it was.

        Collection runs this while holding the collector lock, so each call
        makes at most one network request, and a binding whose probe failed is
        tried again only BOT_ADMIN_RETRY seconds after it ended. A slow or
        failing probe therefore cannot delay intake on every collection.
        """
        now = self.clock()
        with self._db() as db:
            pending = db.execute('SELECT b.client_id,b.chat_id FROM bindings b JOIN clients c ON c.id=b.client_id '
                                 'WHERE b.connected=1 AND c.active=1 AND ('
                                 '(b.bot_admin IS NULL AND (b.bot_admin_probed IS NULL OR b.bot_admin_probed<=?)) '
                                 'OR (b.bot_admin IS NOT NULL AND (b.bot_admin_probed IS NULL OR b.bot_admin_probed<=?))) '
                                 'ORDER BY b.bot_admin IS NOT NULL, b.bot_admin_probed IS NOT NULL, b.bot_admin_probed LIMIT 1',
                                 (now - self.BOT_ADMIN_RETRY, now - self.BOT_ADMIN_RECHECK)).fetchall()
        for row in pending:
            admin = None
            try:
                member = self.transport.get_chat_member(row['chat_id'], account['bot_id'])
                admin = 1 if isinstance(member, dict) and member.get('status') == 'administrator' else 0
            except TransportError as error:
                if isinstance(error.code, int) and 400 <= error.code < 500 and error.code != 429:
                    admin = 0
            except Exception:
                admin = None
            with self._tx() as db:
                if admin is None:
                    # A failed check keeps the flag; a known one is retried at the next recheck.
                    db.execute('UPDATE bindings SET bot_admin_probed=? WHERE client_id=? AND connected=1',
                               (self.clock(), row['client_id']))
                else:
                    db.execute('UPDATE bindings SET bot_admin=?,bot_admin_probed=? WHERE client_id=? AND connected=1',
                               (admin, self.clock(), row['client_id']))

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

        A cursor freezes the event window and row membership for 15 minutes,
        except that context older than the history window can be pruned between
        pages and then drops out. Acknowledgment and publication statuses reflect
        the time of each read.
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
                                   status='context' if row['context'] else 'acknowledged' if row['acknowledged'] else 'pending',
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

    def clear_suspension(self, client_id, *, topic_open_confirmed=False):
        """Local owner control; never exposed as an agent MCP tool.

        Ask Telegram first whether the bot can post in the bound chat again.
        Clear the flag only if no suspension event arrived during the check,
        including a repeat of the same reason, so a newer one stays in place. Queued inbox and outbox
        rows are not touched; they are delivered once the binding is usable.
        """
        if not isinstance(topic_open_confirmed, bool):
            raise CoreError('invalid_unsuspend')
        with self._db() as db:
            binding = self._binding(db, client_id)
            account = self._account(db)
        reason, seq = binding['suspended'], binding['suspension_seq']
        if not reason:
            raise CoreError('not_suspended')
        migrated_to = None
        if reason.startswith('group_migrated:'):
            # The destination itself changed. Clearing must move the binding to
            # the new chat id; the old one no longer exists.
            target = reason.split(':', 1)[1]
            if not re.fullmatch(r'-?[0-9]+', target):
                raise CoreError('unknown_suspension')
            migrated_to = int(target)
        elif reason not in ('bot_removed_or_restricted', 'topic_changed'):
            raise CoreError('unknown_suspension')
        if reason == 'topic_changed' and not topic_open_confirmed:
            # The Bot API has no read-only call for one forum topic's state.
            raise CoreError('topic_open_confirmation_required')
        try:
            member = self.transport.get_chat_member(migrated_to or binding['chat_id'], account['bot_id'])
        except TransportError as error:
            if isinstance(error.code, int) and 400 <= error.code < 500 and error.code != 429:
                raise CoreError('bot_not_in_chat') from None
            raise CoreError('telegram_check_failed') from None
        status = member.get('status') if isinstance(member, dict) else None
        if not (status in ('member', 'administrator')
                or (status == 'restricted' and member.get('is_member') is True
                    and member.get('can_send_messages') is True)):
            raise CoreError('bot_cannot_post')
        with self._tx() as db:
            # The suspension check also refreshes what reactions can arrive here.
            db.execute('UPDATE bindings SET bot_admin=?,bot_admin_probed=? WHERE client_id=? AND connected=1',
                       (1 if status == 'administrator' else 0, self.clock(), client_id))
            current = db.execute('SELECT * FROM bindings WHERE client_id=? AND connected=1 AND suspended=? AND suspension_seq=?',
                                 (client_id, reason, seq)).fetchone()
            if current is None:
                raise CoreError('suspension_changed')
            if migrated_to is not None:
                if db.execute('SELECT 1 FROM bindings WHERE chat_id=? AND thread_id=? AND connected=1',
                              (migrated_to, current['thread_id'])).fetchone():
                    raise CoreError('destination_already_bound')
                self._move_binding(db, current, migrated_to)
            else:
                db.execute('UPDATE bindings SET suspended=NULL WHERE client_id=? AND connected=1', (client_id,))
        status_after = self.connection_status(client_id)
        return {'client_id': client_id, 'previous_suspension': reason, 'suspended': None,
                'paused': status_after['paused'], 'state': status_after['state'],
                'verified': {'bot_member_status': status, 'topic_open': 'owner_confirmed' if reason == 'topic_changed' else None},
                'pending_messages': status_after['pending_messages'],
                'queued_outgoing': status_after['outbox'].get('queued', 0)}

    def disconnect(self, client_id):
        with self._tx() as db:
            self._client(db, client_id)
            images = [row['image'] for row in db.execute(
                'SELECT image FROM outbox WHERE status=\'queued\' AND (client_id=? OR recipient=?) AND image IS NOT NULL', (client_id, client_id))]
            db.execute('UPDATE outbox SET status=\'rejected\',error=\'client_revoked\',updated=? WHERE status=\'queued\' AND (client_id=? OR recipient=?)', (self.clock(), client_id, client_id))
            db.execute('UPDATE reaction_outbox SET status=\'rejected\',error=\'client_revoked\',updated=? WHERE status=\'queued\' AND client_id=?', (self.clock(), client_id))
            db.execute('UPDATE owner_outbox SET status=\'rejected\',error=\'client_revoked\',updated=? WHERE status=\'queued\' AND client_id=?', (self.clock(), client_id))
            db.execute('UPDATE bindings SET connected=0 WHERE client_id=?', (client_id,))
            db.execute('UPDATE clients SET active=0 WHERE id=?', (client_id,))
            db.execute('UPDATE pairings SET consumed=1 WHERE client_id=?', (client_id,))
        for image in images:
            self._delete_outbox_image(image)
        return {'client_id': client_id, 'connected': False, 'revoked': True, 'queued_work_preserved': True}

    def _conversation_send_limit(self, db, conversation_id, now, limits):
        # A grant raises the limit for every sender in the conversation, because
        # every sender's reservations count toward the same window. Only a
        # client that already takes part in the conversation can create one.
        extra = db.execute(
            'SELECT MAX(additional_sends) FROM conversation_budget_grants '
            'WHERE conversation_id=? AND created<=? AND expires_at>?',
            (conversation_id, now, now)).fetchone()[0]
        return limits['conversation_send_limit'] + (extra or 0)

    def conversation_budget(self, client_id, conversation_id, *, additional_sends=None,
                            duration_seconds=None, reason=None):
        """Inspect or grant a temporary higher rolling limit through the local operator CLI."""
        if conversation_id is None:
            raise CoreError('invalid_conversation')
        changing = additional_sends is not None
        if changing:
            if (not _integer(additional_sends) or not 1 <= additional_sends <= 32
                    or not _integer(duration_seconds) or not 1 <= duration_seconds <= 86400
                    or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500):
                raise CoreError('invalid_conversation_budget')
        elif duration_seconds is not None or reason is not None:
            raise CoreError('invalid_conversation_budget')
        now = self.clock()
        with self._tx() as db:
            self._binding(db, client_id, usable=changing)
            conversation = self._conversation(db, client_id, conversation_id)
            row = db.execute('SELECT send_count FROM conversations WHERE id=?', (conversation,)).fetchone()
            count = row['send_count'] if row else 0
            limits = self._limits(db)
            if changing:
                db.execute('INSERT OR IGNORE INTO conversations(id) VALUES(?)', (conversation,))
                # A grant raises this conversation's rolling-window limit while it is active.
                db.execute(
                    'INSERT INTO conversation_budget_grants'
                    '(client_id,conversation_id,send_limit,created,expires_at,reason,additional_sends)'
                    ' VALUES(?,?,?,?,?,?,?)',
                    (client_id, conversation, limits['conversation_send_limit'] + additional_sends,
                     now, now + duration_seconds, reason.strip(), additional_sends))
            grants = [dict(row) for row in db.execute(
                'SELECT id,additional_sends,created,expires_at,reason FROM conversation_budget_grants '
                'WHERE client_id=? AND conversation_id=? ORDER BY id', (client_id, conversation))]
            limit = self._conversation_send_limit(db, conversation, now, limits)
            used, retry_at = self._conversation_usage(db, conversation, now, limit, limits)
            reset = db.execute('SELECT window_reset_at FROM conversations WHERE id=?', (conversation,)).fetchone()
            reset_at = reset[0] if reset else None
        return {'conversation_id': conversation, 'send_count': count, 'send_limit': limit,
                'window_seconds': limits['conversation_window_seconds'], 'sent_in_window': used,
                'window_reset_at': reset_at,
                'remaining_sends': max(0, limit - used), 'retry_at': retry_at, 'grants': grants}

    def _delete_attachment_files(self, delivery_ids):
        directory = self.state_dir / 'attachments'
        prefixes = tuple(f'{delivery_id}-' for delivery_id in delivery_ids)
        try:
            names = os.listdir(directory)
        except FileNotFoundError:
            return
        for name in names:
            if name.startswith(prefixes):
                with contextlib.suppress(FileNotFoundError):
                    (directory / name).unlink()

    def fetch_attachment(self, client_id, delivery_id, file_unique_id):
        """Download one attachment of this client's captured message into private local state.

        The file is named from the delivery and Telegram's unique file id, never
        from the sender's file name. A second fetch reuses the stored file.
        """
        if (not isinstance(delivery_id, str) or not re.fullmatch(r'msg_[0-9a-f]{24}', delivery_id)
                or not isinstance(file_unique_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', file_unique_id)):
            raise CoreError('invalid_attachment')
        with self._db() as db:
            self._binding(db, client_id)
            row = db.execute('SELECT payload FROM inbox WHERE id=? AND client_id=?', (delivery_id, client_id)).fetchone()
        payload = json.loads(row['payload']) if row else {}
        # A reply's quoted message carries its own attachments; they are fetched the same way.
        candidates = [*payload.get('attachments', []), *_mapping(payload.get('reply_to_message')).get('attachments', [])]
        item = next((a for a in candidates if a.get('file_unique_id') == file_unique_id), None)
        if item is None:
            raise CoreError('unknown_attachment')
        # Context capture keeps such messages readable; it never lets their files in.
        if payload.get('context_reason') == 'sender_not_allowed':
            raise CoreError('attachment_sender_not_allowed')
        if item.get('file_size', 0) > MAX_ATTACHMENT_BYTES:
            raise CoreError('attachment_too_large')
        try:
            directory = private_directory(self.state_dir / 'attachments')
        except CredentialError:
            raise CoreError('insecure_state_directory') from None
        target = directory / f'{delivery_id}-{file_unique_id}'
        try:
            info = target.lstat()
        except FileNotFoundError:
            try:
                data = self.transport.download_file(item['file_id'], MAX_ATTACHMENT_BYTES)
            except TransportError as error:
                raise CoreError('attachment_too_large' if error.code == 'file_too_large' else 'attachment_unavailable') from None
            if not isinstance(data, bytes) or len(data) > MAX_ATTACHMENT_BYTES:
                raise CoreError('attachment_unavailable')
            partial = directory / f'.{target.name}.{secrets.token_hex(8)}.partial'
            fd = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(partial, target)
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    partial.unlink()
                raise
            info = target.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise CoreError('insecure_attachment_file')
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        result = {'delivery_id': delivery_id, 'file_unique_id': file_unique_id, 'kind': item['kind'],
                  'path': str(target), 'size': info.st_size, 'sha256': digest, 'content_trust': 'untrusted'}
        for key in ('file_name', 'mime_type'):
            if key in item:
                result[key] = item[key]
        return result

    def _conversation(self, db, client_id, conversation_id):
        if conversation_id is not None:
            if not isinstance(conversation_id, str) or len(conversation_id) > 200:
                raise CoreError('invalid_conversation')
            if not db.execute('SELECT 1 FROM inbox WHERE client_id=? AND conversation_id=? UNION SELECT 1 FROM outbox WHERE client_id=? AND conversation_id=? LIMIT 1', (client_id, conversation_id, client_id, conversation_id)).fetchone():
                raise CoreError('unknown_conversation')
            return conversation_id
        # Continue the most recent admitted conversation, including broker work.
        # Explicit conversation IDs always keep their own rolling send budget.
        row = db.execute('SELECT conversation_id FROM inbox WHERE client_id=? AND context=0 ORDER BY created DESC,rowid DESC LIMIT 1', (client_id,)).fetchone()
        if row:
            return row['conversation_id']
        return 'local:' + client_id

    def _read_image(self, image_path):
        """Validate one agent-provided local image and load its bytes.

        The check runs before any state is written, so a rejected image never
        reaches the outbox or Telegram. The file is opened once with
        O_NONBLOCK and classified through fstat on that descriptor: a path
        swapped for a FIFO or device between lookup and open cannot block the
        call, and only a regular file is ever read.
        """
        if (not isinstance(image_path, str) or not image_path or len(image_path) > 4096
                or '\x00' in image_path or not os.path.isabs(image_path)):
            raise CoreError('invalid_image')
        try:
            # O_NONBLOCK never affects regular-file reads; it only stops a
            # swapped-in FIFO or device from blocking this open forever.
            fd = os.open(image_path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            raise CoreError('invalid_image') from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise CoreError('invalid_image')
            if info.st_size > MAX_IMAGE_BYTES:
                raise CoreError('image_too_large')
            with os.fdopen(fd, 'rb', closefd=False) as handle:
                data = handle.read(MAX_IMAGE_BYTES + 1)
        except OSError:
            raise CoreError('invalid_image') from None
        finally:
            os.close(fd)
        if len(data) > MAX_IMAGE_BYTES:
            raise CoreError('image_too_large')
        send_as, extension, mime = _image_type(data)
        if send_as is None:
            raise CoreError('unsupported_image_type')
        return {'data': data, 'sha256': hashlib.sha256(data).hexdigest(),
                'send_as': send_as, 'extension': extension, 'mime': mime}

    def _store_outbox_image(self, name, data):
        """Persist the retry copy of an image under the private state directory."""
        try:
            directory = private_directory(self.state_dir / 'outbox-images')
        except CredentialError:
            raise CoreError('insecure_state_directory') from None
        partial = directory / f'.{name}.{secrets.token_hex(8)}.partial'
        fd = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(partial, directory / name)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                partial.unlink()
            raise

    def _delete_outbox_image(self, name):
        # Terminal records can never be sent again; drop their retry copy.
        if isinstance(name, str) and re.fullmatch(r'out_[0-9a-f]{24}\.(png|jpg|webp|gif)', name):
            with contextlib.suppress(OSError):
                (self.state_dir / 'outbox-images' / name).unlink()

    def send(self, client_id, text, idempotency_key, recipient=None, reply_to=None, conversation_id=None, image_path=None):
        if not isinstance(text, str) or not 1 <= len(text) <= 3500:
            raise CoreError('invalid_text')
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
            raise CoreError('invalid_idempotency_key')
        if reply_to is not None and (not _integer(reply_to) or reply_to <= 0):
            raise CoreError('invalid_reply')
        if recipient is not None and (not isinstance(recipient, str) or len(recipient) > 100):
            raise CoreError('invalid_peer')
        image = self._read_image(image_path) if image_path is not None else None
        if image is not None and len(text) > MAX_IMAGE_CAPTION:
            # The label header consumes more; the exact check runs before queuing.
            raise CoreError('caption_too_long')
        # Text-only requests keep their historical hash, so old keys still match.
        parts = [text, recipient, reply_to, conversation_id]
        if image is not None:
            parts.append(image['sha256'])
        request_hash = _digest(json.dumps(parts))
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
                limits = self._limits(db)
                limit = self._conversation_send_limit(db, conversation, now, limits)
                used, retry_at = self._conversation_usage(db, conversation, now, limit, limits)
                if used >= limit:
                    raise self._limit_error('conversation_limit', used, limit,
                                            limits['conversation_window_seconds'], retry_at, now)
                used, retry_at = self._client_usage(db, client_id, now, limits)
                if used >= limits['client_send_limit']:
                    raise self._limit_error('client_limit', used, limits['client_send_limit'],
                                            limits['client_window_seconds'], retry_at, now)
                full = []
                for pair in db.execute('SELECT client_id,sender_id FROM bot_exchange_members WHERE conversation_id=?',
                                       (conversation,)).fetchall():
                    used, retry_at = self._bot_pair_usage(db, pair['client_id'], pair['sender_id'], now, limits)
                    if used >= limits['pair_send_limit']:
                        full.append((retry_at, used))
                if full:
                    # Report the latest time, when every full pair has room again.
                    retry_at, used = max(full)
                    raise self._limit_error('bot_pair_limit', used, limits['pair_send_limit'],
                                            limits['pair_window_seconds'], retry_at, now)
                outbox_id = 'out_' + secrets.token_hex(12)
                publication = f'[Agent: {client["label"]} | shared bot @{account["username"]}]\n{text}'
                peer = db.execute('SELECT * FROM peer_commands WHERE client_id=?', (client_id,)).fetchone() if recipient is None else None
                if peer:
                    publication = f'/{peer["command"]}@{peer["bot_username"]}\n' + publication
                if image is not None and len(publication) > MAX_IMAGE_CAPTION:
                    raise CoreError('caption_too_long')
                stored_image = outbox_id + '.' + image['extension'] if image is not None else None
                if stored_image is not None:
                    # The retry copy is written before the row exists, so a queued
                    # or unknown record never depends on the agent's source file.
                    self._store_outbox_image(stored_image, image['data'])
                try:
                    db.execute('INSERT INTO outbox(id,client_id,recipient,chat_id,thread_id,idempotency_key,request_hash,text,reply_to,conversation_id,status,image,created,updated,due) VALUES(?,?,?,?,?,?,?,?,?,?,\'queued\',?,?,?,?)', (outbox_id, client_id, recipient, binding['chat_id'], binding['thread_id'], idempotency_key, request_hash, publication, reply_to, conversation, stored_image, now, now, now))
                    db.execute('UPDATE conversations SET send_count=send_count+1 WHERE id=?', (conversation,))
                    if recipient:
                        payload = {'origin': 'broker', 'kind': 'message', 'text': text, 'logical_sender': {'client_id': client_id, 'label': client['label']}, 'actual_bot': {'id': account['bot_id'], 'username': account['username']}, 'chat_id': binding['chat_id'], 'thread_id': binding['thread_id'] or None}
                        self._enqueue(db, recipient, 'broker:' + outbox_id, payload, conversation, now, outbox_id)
                except BaseException:
                    if stored_image is not None:
                        self._delete_outbox_image(stored_image)
                    raise
        self.flush(client_id=client_id, limit=20)
        return self.delivery_status(client_id, outbox_id)

    def _known_reaction_target(self, db, binding, message_id):
        """True when the message id was captured or published in this destination.

        A reaction can only target the agent's own bound group or topic: either a
        message this destination captured, or any message the shared bot
        published there, including one published for a peer agent.
        """
        if db.execute('SELECT 1 FROM outbox WHERE chat_id=? AND thread_id=? AND message_id=? LIMIT 1',
                      (binding['chat_id'], binding['thread_id'], message_id)).fetchone():
            return True
        for row in db.execute('SELECT payload FROM inbox WHERE client_id=?', (binding['client_id'],)):
            payload = json.loads(row['payload'])
            if (payload.get('message_id') == message_id and payload.get('chat_id') == binding['chat_id']
                    and (payload.get('thread_id') or 0) == binding['thread_id']):
                return True
        return False

    def _reaction_usage(self, db, client_id, now):
        stamps = [r[0] for r in db.execute(
            'SELECT created FROM reaction_outbox WHERE client_id=? AND created>? ORDER BY created DESC',
            (client_id, now - self.REACTION_WINDOW))]
        return self._window_usage(stamps, self.REACTION_SEND_LIMIT, self.REACTION_WINDOW)

    def react(self, client_id, message_id, emoji, idempotency_key):
        """Queue one reaction on a known message in this agent's own destination.

        Reactions post no message, so they never touch any message send limit;
        they carry their own per-agent rolling limit. Like message_send, the
        intent is durable before any network activity, a repeated idempotency key
        with the same content returns the existing outcome, and an uncertain
        delivery needs private operator resolution.
        """
        if not _integer(message_id) or message_id <= 0:
            raise CoreError('invalid_reaction')
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
            raise CoreError('invalid_idempotency_key')
        emoji = reaction_emoji(emoji)
        request_hash = _digest(json.dumps([message_id, emoji]))
        now = self.clock()
        with self._tx() as db:
            self._client(db, client_id)
            existing = db.execute('SELECT * FROM reaction_outbox WHERE client_id=? AND idempotency_key=?',
                                  (client_id, idempotency_key)).fetchone()
            if existing:
                if existing['request_hash'] != request_hash:
                    raise CoreError('idempotency_conflict')
                reaction_id = existing['id']
            else:
                binding = self._binding(db, client_id, usable=True)
                if not self._known_reaction_target(db, binding, message_id):
                    raise CoreError('unknown_reaction_target')
                used, retry_at = self._reaction_usage(db, client_id, now)
                if used >= self.REACTION_SEND_LIMIT:
                    raise self._limit_error('reaction_limit', used, self.REACTION_SEND_LIMIT,
                                            self.REACTION_WINDOW, retry_at, now)
                reaction_id = 'react_' + secrets.token_hex(12)
                db.execute("INSERT INTO reaction_outbox(id,client_id,chat_id,thread_id,message_id,emoji,"
                           'idempotency_key,request_hash,status,created,updated,due) '
                           "VALUES(?,?,?,?,?,?,?,?,'queued',?,?,?)",
                           (reaction_id, client_id, binding['chat_id'], binding['thread_id'], message_id,
                            emoji, idempotency_key, request_hash, now, now, now))
        self.flush(client_id=client_id, limit=20)
        return self.delivery_status(client_id, reaction_id)

    def _owner_notify_usage(self, db, client_id, now):
        """The fullest owner_notify window as (used, limit, window, retry_at), or None.

        Every request that was not refused counts, including queued and unknown
        ones: an uncertain notification may already have reached the owner.
        A refused one never reached the owner, so it does not count.
        """
        full = []
        for limit, window in self.OWNER_NOTIFY_LIMITS:
            stamps = [r[0] for r in db.execute(
                "SELECT created FROM owner_outbox WHERE client_id=? AND created>? AND status!='rejected' "
                'ORDER BY created DESC', (client_id, now - window))]
            used, retry_at = self._window_usage(stamps, limit, window)
            if retry_at is not None:
                full.append((retry_at, used, limit, window))
        if not full:
            return None
        # Report the latest time, when every full window has room again.
        retry_at, used, limit, window = max(full)
        return used, limit, window, retry_at

    def owner_notify(self, client_id, text, idempotency_key, forward_message_id=None):
        """Queue one low-volume notification to the owner's private chat with the bot.

        The owner is the person who paired this destination; in a private chat
        Telegram uses the user id as the chat id. A forward copies one message
        from this agent's own bound destination, by the same rule as reactions.
        Like message_send, the intent is durable before any network activity,
        and a repeated idempotency key with the same content returns the same
        outcome.
        """
        if not isinstance(text, str) or not 1 <= len(text) <= 3500:
            raise CoreError('invalid_text')
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
            raise CoreError('invalid_idempotency_key')
        if forward_message_id is not None and (not _integer(forward_message_id) or forward_message_id <= 0):
            raise CoreError('invalid_forward')
        request_hash = _digest(json.dumps([text, forward_message_id]))
        now = self.clock()
        with self._tx() as db:
            client = self._client(db, client_id)
            existing = db.execute('SELECT * FROM owner_outbox WHERE client_id=? AND idempotency_key=?',
                                  (client_id, idempotency_key)).fetchone()
            if existing:
                if existing['request_hash'] != request_hash:
                    raise CoreError('idempotency_conflict')
                notify_id = existing['id']
            else:
                binding = self._binding(db, client_id, usable=True)
                if not binding['owner_notify']:
                    # Nothing is stored, so this refusal never counts toward a limit.
                    raise CoreError('owner_notify_disabled')
                if forward_message_id is not None and not self._known_reaction_target(db, binding, forward_message_id):
                    raise CoreError('unknown_forward_target')
                usage = self._owner_notify_usage(db, client_id, now)
                if usage is not None:
                    used, limit, window, retry_at = usage
                    raise self._limit_error('owner_notify_limit', used, limit, window, retry_at, now)
                notify_id = 'owner_' + secrets.token_hex(12)
                # The owner reads this away from the group, so the label names
                # the group instead of the bot, which is the private chat itself.
                # A reply here cannot reach one agent when several share the bot.
                # Pairing already strips control characters from the title; collapse
                # any whitespace too, so a group name can't fake a label or footer line.
                title = ' '.join(str(binding['title']).split())
                labelled = f'[Agent: {client["label"]} | {title}]\n{text}\n{self.OWNER_NOTIFY_FOOTER}'
                db.execute("INSERT INTO owner_outbox(id,client_id,owner_id,text,from_chat_id,forward_message_id,"
                           'idempotency_key,request_hash,status,created,updated,due) '
                           "VALUES(?,?,?,?,?,?,?,?,'queued',?,?,?)",
                           (notify_id, client_id, binding['owner_id'], labelled,
                            binding['chat_id'] if forward_message_id is not None else None, forward_message_id,
                            idempotency_key, request_hash, now, now, now))
            username = self._account(db)['username']
        self.flush(client_id=client_id, limit=20)
        status = self.delivery_status(client_id, notify_id)
        if status['error'] == 'owner_chat_not_started':
            # Only the validated bot username goes into the details.
            raise CoreError('owner_chat_not_started', details={'bot_username': username})
        return status

    @staticmethod
    def _owner_status(row):
        return {'notify_id': row['id'], 'kind': 'owner_notify', 'status': row['status'],
                'error': row['error'], 'message_id': row['message_id'],
                'forward_message_id': row['forward_message_id'],
                'forwarded_message_id': row['forwarded_message_id'],
                'created_at': row['created'], 'updated_at': row['updated'],
                'retry_at': row['due'] if row['status'] == 'queued' else None}

    @staticmethod
    def _reaction_status(row):
        return {'reaction_id': row['id'], 'kind': 'reaction', 'status': row['status'],
                'error': row['error'], 'emoji': row['emoji'], 'message_id': row['message_id'],
                'chat_id': row['chat_id'], 'thread_id': row['thread_id'] or None,
                'created_at': row['created'], 'updated_at': row['updated'],
                'retry_at': row['due'] if row['status'] == 'queued' else None}

    def _outbox_status(self, row):
        return {'outbox_id': row['id'], 'status': row['status'], 'error': row['error'], 'message_id': row['message_id'], 'recipient': row['recipient'], 'conversation_id': row['conversation_id'], 'created_at': row['created'], 'updated_at': row['updated'], 'retry_at': row['due'] if row['status'] == 'queued' else None}

    def delivery_status(self, client_id, outbox_id):
        """Inspect an outgoing message or reaction delivery; never resends it."""
        with self._db() as db:
            self._client(db, client_id)
            row = db.execute('SELECT * FROM outbox WHERE client_id=? AND id=?', (client_id, outbox_id)).fetchone()
            if row is not None:
                return self._outbox_status(row)
            row = db.execute('SELECT * FROM reaction_outbox WHERE client_id=? AND id=?', (client_id, outbox_id)).fetchone()
            if row is not None:
                return self._reaction_status(row)
            row = db.execute('SELECT * FROM owner_outbox WHERE client_id=? AND id=?', (client_id, outbox_id)).fetchone()
            if row is None:
                raise CoreError('unknown_delivery')
            return self._owner_status(row)

    def _recover_sends(self, db):
        # Only call while holding sender.lock: no live local sender can own a row.
        db.execute('UPDATE outbox SET status=\'unknown\',error=\'interrupted_send\',updated=? WHERE status=\'sending\'', (self.clock(),))
        db.execute('UPDATE reaction_outbox SET status=\'unknown\',error=\'interrupted_send\',updated=? WHERE status=\'sending\'', (self.clock(),))
        db.execute('UPDATE owner_outbox SET status=\'unknown\',error=\'interrupted_send\',updated=? WHERE status=\'sending\'', (self.clock(),))

    def flush(self, client_id=None, limit=20):
        if not _integer(limit) or not 1 <= limit <= 100:
            raise CoreError('invalid_flush_limit')
        if client_id is not None:
            with self._db() as db:
                self._client(db, client_id)
        with self._lock('sender') as acquired:
            if not acquired:
                return {'status': 'busy', 'attempted': 0, 'reactions': 0, 'owner_notifications': 0}
            with self._tx() as db:
                self._recover_sends(db)
                rows = db.execute('SELECT id FROM outbox WHERE status=\'queued\' AND due<=? AND (? IS NULL OR client_id=?) ORDER BY created,rowid LIMIT ?', (self.clock(), client_id, client_id, limit)).fetchall()
                reactions = db.execute('SELECT id FROM reaction_outbox WHERE status=\'queued\' AND due<=? AND (? IS NULL OR client_id=?) ORDER BY created,rowid LIMIT ?', (self.clock(), client_id, client_id, limit)).fetchall()
                notes = db.execute('SELECT id FROM owner_outbox WHERE status=\'queued\' AND due<=? AND (? IS NULL OR client_id=?) ORDER BY created,rowid LIMIT ?', (self.clock(), client_id, client_id, limit)).fetchall()
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
                media = None
                local_reject = None
                migrated_to = None
                if row['image']:
                    try:
                        data = (self.state_dir / 'outbox-images' / row['image']).read_bytes()
                        send_as, _extension, mime = _image_type(data)
                        if send_as is None:
                            raise ValueError('corrupt copy')
                    except (OSError, ValueError):
                        # Our own durable copy is gone or unreadable, so this
                        # attempt certainly never reached Telegram.
                        local_reject = 'outbox_image_unavailable'
                    else:
                        media = (data, send_as, mime)
                if local_reject is not None:
                    status, error, message_id, due = 'rejected', local_reject, None, self.clock()
                else:
                    try:
                        if media is None:
                            response = self.transport.send_message(chat_id=row['chat_id'], text=row['text'], thread_id=row['thread_id'] or None, reply_to=row['reply_to'])
                        else:
                            sender = self.transport.send_photo if media[1] == 'photo' else self.transport.send_document
                            response = sender(chat_id=row['chat_id'], data=media[0], caption=row['text'], filename=row['image'], mime=media[2], thread_id=row['thread_id'] or None, reply_to=row['reply_to'])
                        if not isinstance(response, dict) or not _integer(response.get('message_id')) or response['message_id'] <= 0:
                            raise UnknownSend()
                    except UnknownSend:
                        status, error, message_id, due = 'unknown', 'uncertain_send', None, self.clock()
                    except TransportError as exc:
                        if str(exc.code) in ('429', 'rate_limited', 'telegram_429'):
                            wait = exc.retry_after if isinstance(exc.retry_after, (int, float)) and not isinstance(exc.retry_after, bool) and math.isfinite(exc.retry_after) and exc.retry_after > 0 else 60
                            due = self.clock() + min(wait, 86400)
                            status, error, message_id = 'queued', 'rate_limited', None
                        elif _integer(exc.migrate_to_chat_id) and exc.migrate_to_chat_id != row['chat_id']:
                            # A definite rejection: the group became a supergroup.
                            # Nothing was sent, so the message waits for the new chat.
                            status, error, message_id, due = 'queued', 'group_migrated', None, self.clock()
                            migrated_to = exc.migrate_to_chat_id
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
                    if error == 'group_migrated':
                        self._follow_send_migration(db, row, migrated_to)
                    if error == 'rate_limited':
                        db.execute('INSERT INTO rate_limits(scope,next_at) VALUES(\'bot\',?) ON CONFLICT(scope) DO UPDATE SET next_at=max(next_at,excluded.next_at)', (due,))
                if status in ('sent', 'rejected') and row['image']:
                    # An unknown record keeps its copy: an operator retry sends it again.
                    self._delete_outbox_image(row['image'])
            reacted = self._flush_reactions(reactions)
            notified = self._flush_owner_notes(notes)
            return {'status': 'ok', 'attempted': attempted, 'reactions': reacted, 'owner_notifications': notified}

    def _flush_reactions(self, candidates):
        """Attempt due queued reactions under the sender lock; returns attempts.

        Reactions pace on their own 'react' scope, never on a message scope. An
        uncertain outcome stays unknown: like a message send, an attempt may have
        reached Telegram after the failure, and re-setting a reaction is left to
        an explicit operator resolution.
        """
        attempted = 0
        for candidate in candidates:
            with self._tx() as db:
                row = db.execute('SELECT * FROM reaction_outbox WHERE id=?', (candidate['id'],)).fetchone()
                if row['status'] != 'queued':
                    continue
                try:
                    self._binding(db, row['client_id'], usable=True)
                except CoreError:
                    continue
                now = self.clock()
                due = now
                rate = db.execute('SELECT next_at FROM rate_limits WHERE scope=\'react\'').fetchone()
                if rate:
                    due = max(due, rate['next_at'])
                if due > now:
                    db.execute('UPDATE reaction_outbox SET due=? WHERE id=?', (due, row['id']))
                    continue
                db.execute('INSERT INTO rate_limits(scope,next_at) VALUES(\'react\',?) ON CONFLICT(scope) DO UPDATE SET next_at=excluded.next_at', (now + self.REACTION_INTERVAL,))
                db.execute('UPDATE reaction_outbox SET status=\'sending\',updated=? WHERE id=?', (now, row['id']))
            attempted += 1
            migrated_to = None
            try:
                self.transport.set_message_reaction(row['chat_id'], row['message_id'], row['emoji'])
                status, error, due = 'sent', None, self.clock()
            except UnknownSend:
                status, error, due = 'unknown', 'uncertain_send', self.clock()
            except TransportError as exc:
                if str(exc.code) in ('429', 'rate_limited', 'telegram_429'):
                    wait = exc.retry_after if isinstance(exc.retry_after, (int, float)) and not isinstance(exc.retry_after, bool) and math.isfinite(exc.retry_after) and exc.retry_after > 0 else 60
                    due = self.clock() + min(wait, 86400)
                    status, error = 'queued', 'rate_limited'
                elif _integer(exc.migrate_to_chat_id) and exc.migrate_to_chat_id != row['chat_id']:
                    # The group became a supergroup. Its old messages stay in the
                    # deactivated group, so this reaction cannot follow them.
                    status, error, due = 'rejected', 'group_migrated', self.clock()
                    migrated_to = exc.migrate_to_chat_id
                elif isinstance(exc.code, int) and 400 <= exc.code < 500:
                    status, error, due = 'rejected', 'telegram_rejected', self.clock()
                else:
                    status, error, due = 'unknown', 'transport_failure', self.clock()
            except Exception:
                status, error, due = 'unknown', 'transport_failure', self.clock()
            with self._tx() as db:
                db.execute('UPDATE reaction_outbox SET status=?,error=?,due=?,updated=? WHERE id=?', (status, error, due, self.clock(), row['id']))
                if migrated_to is not None:
                    self._follow_migration(db, row['chat_id'], migrated_to)
                if error == 'rate_limited':
                    db.execute('INSERT INTO rate_limits(scope,next_at) VALUES(\'react\',?) ON CONFLICT(scope) DO UPDATE SET next_at=max(next_at,excluded.next_at)', (due,))
        return attempted

    def _flush_owner_notes(self, candidates):
        """Attempt due owner notifications under the sender lock; returns attempts.

        A notification has up to two steps: the labelled text, then the
        optional forward. Each step records its message id when it succeeds,
        so a retry after an unknown outcome repeats only the missing step.
        Pacing uses one scope per owner private chat.
        """
        attempted = 0
        for candidate in candidates:
            with self._tx() as db:
                row = db.execute('SELECT * FROM owner_outbox WHERE id=?', (candidate['id'],)).fetchone()
                if row['status'] != 'queued':
                    continue
                try:
                    self._binding(db, row['client_id'], usable=True)
                except CoreError:
                    continue
                now = self.clock()
                scope = f'owner:{row["owner_id"]}'
                rate = db.execute('SELECT next_at FROM rate_limits WHERE scope=?', (scope,)).fetchone()
                due = max(now, rate['next_at']) if rate else now
                if due > now:
                    db.execute('UPDATE owner_outbox SET due=? WHERE id=?', (due, row['id']))
                    continue
                db.execute('INSERT INTO rate_limits(scope,next_at) VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET next_at=excluded.next_at',
                           (scope, now + self.OWNER_NOTIFY_INTERVAL))
                db.execute("UPDATE owner_outbox SET status='sending',updated=? WHERE id=?", (now, row['id']))
            attempted += 1
            message_id, forwarded_id = row['message_id'], row['forwarded_message_id']
            step = 'text' if message_id is None else 'forward'
            try:
                if message_id is None:
                    response = self.transport.send_message(chat_id=row['owner_id'], text=row['text'])
                    if not isinstance(response, dict) or not _integer(response.get('message_id')) or response['message_id'] <= 0:
                        raise UnknownSend()
                    message_id = response['message_id']
                    # Record the text at once, so a failure in the forward
                    # below can never send the text a second time.
                    with self._tx() as db:
                        db.execute('UPDATE owner_outbox SET message_id=?,updated=? WHERE id=?', (message_id, self.clock(), row['id']))
                    step = 'forward'
                if row['forward_message_id'] is not None and forwarded_id is None:
                    response = self.transport.forward_message(chat_id=row['owner_id'], from_chat_id=row['from_chat_id'],
                                                              message_id=row['forward_message_id'])
                    if not isinstance(response, dict) or not _integer(response.get('message_id')) or response['message_id'] <= 0:
                        raise UnknownSend()
                    forwarded_id = response['message_id']
                status, error, due = 'sent', None, self.clock()
            except UnknownSend:
                status, error, due = 'unknown', 'uncertain_send', self.clock()
            except TransportError as exc:
                if str(exc.code) in ('429', 'rate_limited', 'telegram_429'):
                    wait = exc.retry_after if isinstance(exc.retry_after, (int, float)) and not isinstance(exc.retry_after, bool) and math.isfinite(exc.retry_after) and exc.retry_after > 0 else 60
                    due = self.clock() + min(wait, 86400)
                    status, error = 'queued', 'rate_limited'
                elif step == 'forward' and isinstance(exc.code, int) and 400 <= exc.code < 500:
                    # The owner already has the text; only the copy of the
                    # group message failed, for example because it was deleted.
                    status, error, due = 'sent', 'forward_rejected', self.clock()
                elif exc.code == 403:
                    # A bot cannot open a private chat: the owner never sent it
                    # /start, or blocked it. Nothing reached the owner.
                    status, error, due = 'rejected', 'owner_chat_not_started', self.clock()
                elif isinstance(exc.code, int) and 400 <= exc.code < 500:
                    status, error, due = 'rejected', 'telegram_rejected', self.clock()
                else:
                    status, error, due = 'unknown', 'transport_failure', self.clock()
            except Exception:
                status, error, due = 'unknown', 'transport_failure', self.clock()
            with self._tx() as db:
                db.execute('UPDATE owner_outbox SET status=?,error=?,message_id=?,forwarded_message_id=?,due=?,updated=? WHERE id=?',
                           (status, error, message_id, forwarded_id, due, self.clock(), row['id']))
                if error == 'rate_limited':
                    db.execute('INSERT INTO rate_limits(scope,next_at) VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET next_at=max(next_at,excluded.next_at)',
                               (f'owner:{row["owner_id"]}', due))
        return attempted

    def resolve_unknown(self, outbox_id, resolution, message_id=None):
        if resolution not in ('accepted', 'cancel', 'retry'):
            raise CoreError('invalid_resolution')
        with self._lock('sender') as acquired:
            if not acquired:
                raise CoreError('sender_busy')
            with self._tx() as db:
                self._recover_sends(db)
                row = db.execute('SELECT * FROM outbox WHERE id=?', (outbox_id,)).fetchone()
                table = 'outbox'
                if row is None:
                    row = db.execute('SELECT * FROM reaction_outbox WHERE id=?', (outbox_id,)).fetchone()
                    table = 'reaction_outbox'
                if row is None:
                    # A retry repeats only the step without a recorded message id.
                    row = db.execute('SELECT * FROM owner_outbox WHERE id=?', (outbox_id,)).fetchone()
                    table = 'owner_outbox'
                if row is None or row['status'] != 'unknown':
                    raise CoreError('not_unknown')
                if resolution == 'accepted' and table == 'outbox' and (not _integer(message_id) or message_id <= 0):
                    raise CoreError('message_id_required')
                if resolution == 'retry':
                    self._binding(db, row['client_id'], usable=True)
                    if table == 'outbox' and row['recipient']:
                        self._binding(db, row['recipient'], usable=True)
                status = {'accepted': 'sent', 'cancel': 'rejected', 'retry': 'queued'}[resolution]
                error = None if resolution == 'accepted' else 'operator_' + resolution
                if table == 'outbox':
                    db.execute('UPDATE outbox SET status=?,error=?,message_id=?,due=?,updated=? WHERE id=?', (status, error, message_id if resolution == 'accepted' else None, self.clock(), self.clock(), outbox_id))
                else:
                    db.execute(f'UPDATE {table} SET status=?,error=?,due=?,updated=? WHERE id=?', (status, error, self.clock(), self.clock(), outbox_id))
                result = db.execute(f'SELECT * FROM {table} WHERE id=?', (outbox_id,)).fetchone()
                resolved = {'outbox': self._outbox_status, 'reaction_outbox': self._reaction_status,
                            'owner_outbox': self._owner_status}[table](result)
            if table == 'outbox' and resolution in ('accepted', 'cancel') and row['image']:
                # Terminal: no later attempt can need the copy again. A retry keeps it.
                self._delete_outbox_image(row['image'])
            return resolved
