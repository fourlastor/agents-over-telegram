"""Offline proof for the optional addressed-only filter, group context capture and attachments."""
import hashlib
import os
import sqlite3
import stat

import pytest

from agents_over_telegram.cli import parser
from agents_over_telegram.core import MAX_ATTACHMENT_BYTES, Core, CoreError
from agents_over_telegram.mcp_server import TOOLS
from agents_over_telegram.push import claim_new, ensure_tables
from test_core import append, bind, env, rows  # noqa: F401 (env is a fixture)


@pytest.fixture
def group(env):
    core, transport, clock = env
    client, _ = bind(core, transport)
    core.allow_sender(client, 202, is_bot=True)
    return core, transport, clock, client


def deliveries(core, client):
    return core.poll(client)['deliveries']


def history(core, client):
    return core.history(client, limit=50)['messages']


def test_defaults_keep_the_original_behaviour(group):
    core, transport, clock, client = group
    assert core.connection_status(client)['routing'] == {'delivery_filter': 'all', 'capture_context': False}
    append(transport, 'plain owner message')
    append(transport, 'stranger message', sender=303)
    core.collect()
    assert [d['text'] for d in deliveries(core, client)] == ['plain owner message']
    assert rows(core, 'SELECT count(*) FROM inbox WHERE context=1') == [(0,)]
    assert rows(core, "SELECT reason FROM unmatched WHERE reason='sender_not_allowed'") == [('sender_not_allowed',)]


def test_addressed_filter_delivers_only_mentions_replies_and_commands(group):
    core, transport, clock, client = group
    assert core.delivery_settings(client, delivery_filter='addressed')['delivery_filter'] == 'addressed'
    bot = transport.username
    delivered = [f'@{bot} please look', f'ping @{bot.upper()}', f'/review@{bot} now', 'thanks']
    ignored = ['general chat', f'@{bot}x is another bot', f'mail me at a@{bot}', f'see /review@{bot}x']
    for text in ignored:
        append(transport, text)
    for text in delivered[:3]:
        append(transport, text)
    append(transport, delivered[3], reply_to_message={'message_id': 1, 'from': {'id': transport.bot_id, 'is_bot': True}})
    core.collect()
    ensure_tables(core)
    assert claim_new(core, client, 'watch')['new'] == 4
    assert [d['text'] for d in deliveries(core, client)] == delivered
    context = [m for m in history(core, client) if m['status'] == 'context']
    assert sorted(m['text'] for m in context) == sorted(ignored)
    assert {m['context_reason'] for m in context} == {'not_addressed'}
    assert core.connection_status(client)['pending_messages'] == 4


def test_context_is_never_polled_announced_or_queue_bound(group):
    core, transport, clock, client = group
    core.delivery_settings(client, delivery_filter='addressed', capture_context=True)
    for n in range(5):
        append(transport, f'chatter {n}')
        append(transport, f'stranger {n}', sender=303)
    core.collect()
    ensure_tables(core)
    assert claim_new(core, client, 'watch') is None
    assert deliveries(core, client) == []
    assert core.connection_status(client)['pending_messages'] == 0
    reasons = sorted(m['context_reason'] for m in history(core, client))
    assert reasons == ['not_addressed'] * 5 + ['sender_not_allowed'] * 5
    assert rows(core, "SELECT count(*) FROM unmatched WHERE reason='sender_not_allowed'") == [(0,)]


def test_capture_context_alone_keeps_full_delivery_for_allowed_senders(group):
    core, transport, clock, client = group
    core.delivery_settings(client, capture_context=True)
    append(transport, 'owner work')
    append(transport, 'stranger aside', sender=303)
    core.collect()
    assert [d['text'] for d in deliveries(core, client)] == ['owner work']
    assert [(m['text'], m['context_reason']) for m in history(core, client) if m['status'] == 'context'] == [
        ('stranger aside', 'sender_not_allowed')]
    core.delivery_settings(client, capture_context=False)
    append(transport, 'later stranger', sender=303)
    core.collect()
    assert 'later stranger' not in [m['text'] for m in history(core, client)]


def test_owner_pause_still_works_under_the_filter(group):
    core, transport, clock, client = group
    core.delivery_settings(client, delivery_filter='addressed')
    append(transport, f'/pause@{transport.username}')
    core.collect()
    assert core.connection_status(client)['paused'] is True


def test_edits_follow_the_original_delivery(group):
    core, transport, clock, client = group
    core.delivery_settings(client, delivery_filter='addressed')
    first = append(transport, f'@{transport.username} start')
    second = append(transport, 'aside')
    core.collect()
    poll = core.poll(client)
    core.ack(client, [d['delivery_id'] for d in poll['deliveries']], poll['lease_token'])
    for original, text in ((first, 'start, edited without the mention'), (second, f'aside, now for @{transport.username}')):
        message = dict(original['message'], text=text, edit_date=original['message'].get('date', 1) + 1)
        transport.updates.append({'update_id': max(u['update_id'] for u in transport.updates) + 1,
                                  'edited_message': message})
    append(transport, 'another aside')
    core.collect()
    assert [(d['kind'], d['text']) for d in deliveries(core, client)] == [
        ('edited', 'start, edited without the mention'), ('edited', f'aside, now for @{transport.username}')]


def test_context_never_becomes_the_default_send_conversation(group):
    core, transport, clock, client = group
    core.delivery_settings(client, delivery_filter='addressed')
    append(transport, f'@{transport.username} task')
    core.collect()
    conversation = deliveries(core, client)[0]['conversation_id']
    append(transport, 'unrelated chatter afterwards')
    core.collect()
    clock.advance(2)
    sent = core.send(client, 'done', 'default-conversation')
    assert rows(core, 'SELECT conversation_id FROM outbox WHERE id=?', (sent['outbox_id'],)) == [(conversation,)]


def test_replying_to_bot_context_stays_inside_the_bot_chain(group):
    core, transport, clock, client = group
    core.delivery_settings(client, delivery_filter='addressed')
    event = append(transport, 'bot chatter', sender=202, bot=True)
    core.collect()
    assert deliveries(core, client) == []
    for n in range(core.MAX_CHAIN):
        core.send(client, 'reply', f'chain-{n}', reply_to=event['message']['message_id'])
        clock.advance(2)
    with pytest.raises(CoreError, match='conversation_limit'):
        core.send(client, 'one more', 'chain-over', reply_to=event['message']['message_id'])


def test_context_expires_with_the_history_window(group):
    core, transport, clock, client = group
    core.delivery_settings(client, delivery_filter='addressed')
    append(transport, 'old chatter', date=int(clock.now))
    append(transport, f'@{transport.username} old work', date=int(clock.now))
    core.collect()
    assert rows(core, 'SELECT count(*) FROM inbox WHERE context=1') == [(1,)]
    clock.advance(core.HISTORY_WINDOW + 10)
    append(transport, 'new chatter', date=int(clock.now))
    core.collect()
    texts = [m['text'] for m in history(core, client)]
    assert 'new chatter' in texts and 'old chatter' not in texts
    assert rows(core, 'SELECT count(*) FROM inbox WHERE context=1') == [(1,)]
    assert rows(core, 'SELECT count(*) FROM inbox WHERE context=0') == [(1,)]


def test_existing_state_migrates_with_original_defaults(group):
    core, transport, clock, client = group
    append(transport, 'before upgrade')
    core.collect()
    with sqlite3.connect(core.database) as db:
        db.execute('ALTER TABLE bindings DROP COLUMN delivery_filter')
        db.execute('ALTER TABLE bindings DROP COLUMN capture_context')
        db.execute('ALTER TABLE inbox DROP COLUMN context')
    core = Core(core.state_dir, transport, clock=clock)
    assert core.connection_status(client)['routing'] == {'delivery_filter': 'all', 'capture_context': False}
    assert [d['text'] for d in deliveries(core, client)] == ['before upgrade']


def test_delivery_settings_are_validated_and_operator_only(group):
    core, transport, clock, client = group
    for bad in ({'delivery_filter': 'mentions'}, {'capture_context': 'on'}):
        with pytest.raises(CoreError, match='invalid_delivery_settings'):
            core.delivery_settings(client, **bad)
    assert 'delivery_settings' not in {tool.name for tool in TOOLS}
    args = parser().parse_args(['delivery-settings', '--client-file', 'c.json', '--filter', 'addressed', '--context', 'on'])
    assert (args.delivery_filter, args.capture_context) == ('addressed', 'on')
    unchanged = parser().parse_args(['delivery-settings', '--client-file', 'c.json'])
    assert (unchanged.delivery_filter, unchanged.capture_context) == (None, None)


def media(transport, *, caption=None, sender=101, **fields):
    number = max((u['update_id'] for u in transport.updates), default=0) + 1
    message = {'message_id': number, 'chat': {'id': -100, 'type': 'supergroup', 'title': 'Test group'},
               'from': {'id': sender, 'is_bot': False, 'username': 'participant'}, **fields}
    if caption is not None:
        message['caption'] = caption
    transport.updates.append({'update_id': number, 'message': message})


def test_media_messages_carry_caption_and_attachment_metadata(group):
    core, transport, clock, client = group
    media(transport, caption='the plan', document={'file_id': 'DOC1', 'file_unique_id': 'uDoc1',
                                                    'file_name': 'plan.md', 'mime_type': 'text/markdown', 'file_size': 12})
    media(transport, photo=[{'file_id': 'small', 'file_unique_id': 'uS', 'file_size': 10},
                            {'file_id': 'large', 'file_unique_id': 'uL', 'file_size': 99}])
    media(transport, sticker={'file_id': 'S', 'file_unique_id': 'uSt'})
    media(transport, document={'file_id': 'BAD', 'file_unique_id': '../x'})
    core.collect()
    got = deliveries(core, client)
    assert [(d['text'], d['attachments']) for d in got] == [
        ('the plan', [{'kind': 'document', 'file_id': 'DOC1', 'file_unique_id': 'uDoc1', 'file_name': 'plan.md',
                       'mime_type': 'text/markdown', 'file_size': 12}]),
        ('', [{'kind': 'photo', 'file_id': 'large', 'file_unique_id': 'uL', 'file_size': 99}])]
    assert rows(core, "SELECT count(*) FROM unmatched WHERE reason='unsupported_message'") == [(2,)]


def test_attachment_fetch_stores_privately_and_reuses_the_file(group):
    core, transport, clock, client = group
    transport.files['DOC1'] = {'content': b'# plan\n'}
    media(transport, caption='see file', document={'file_id': 'DOC1', 'file_unique_id': 'uDoc1',
                                                    'file_name': '../../escape.md', 'file_size': 7})
    core.collect()
    delivery = deliveries(core, client)[0]
    result = core.fetch_attachment(client, delivery['delivery_id'], 'uDoc1')
    path = result['path']
    assert os.path.dirname(path) == str(core.state_dir / 'attachments')
    assert os.path.basename(path) == f'{delivery["delivery_id"]}-uDoc1'
    assert result['sha256'] == hashlib.sha256(b'# plan\n').hexdigest() and result['size'] == 7
    assert result['file_name'] == '../../escape.md' and result['content_trust'] == 'untrusted'
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    transport.files.clear()
    assert core.fetch_attachment(client, delivery['delivery_id'], 'uDoc1')['sha256'] == result['sha256']
    with pytest.raises(CoreError, match='unknown_attachment'):
        core.fetch_attachment(client, delivery['delivery_id'], 'other')
    with pytest.raises(CoreError, match='invalid_attachment'):
        core.fetch_attachment(client, '../etc', 'uDoc1')


def test_attachment_size_limits_and_context_files(group):
    core, transport, clock, client = group
    core.delivery_settings(client, delivery_filter='addressed')
    transport.files['BIG'] = {'content': b'x' * (MAX_ATTACHMENT_BYTES + 1)}
    transport.files['CTX'] = {'content': b'context file'}
    media(transport, caption=f'@{transport.username} big', document={'file_id': 'BIG', 'file_unique_id': 'uBig'})
    media(transport, caption=f'@{transport.username} declared', document={
        'file_id': 'HUGE', 'file_unique_id': 'uHuge', 'file_size': MAX_ATTACHMENT_BYTES + 1})
    media(transport, caption='not addressed', document={'file_id': 'CTX', 'file_unique_id': 'uCtx'})
    core.collect()
    big, declared = deliveries(core, client)
    for delivery, unique in ((big, 'uBig'), (declared, 'uHuge')):
        with pytest.raises(CoreError, match='attachment_too_large'):
            core.fetch_attachment(client, delivery['delivery_id'], unique)
    assert not list((core.state_dir / 'attachments').glob('*'))
    context = next(m for m in history(core, client) if m['status'] == 'context')
    assert core.fetch_attachment(client, context['delivery_id'], 'uCtx')['size'] == len(b'context file')


def test_attachment_fetch_is_an_mcp_tool():
    tool = next(tool for tool in TOOLS if tool.name == 'attachment_fetch')
    assert set(tool.inputSchema['required']) == {'delivery_id', 'file_unique_id'}
    assert tool.annotations.openWorldHint is True
