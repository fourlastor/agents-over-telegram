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


def test_defaults_deliver_everything_from_allowed_senders_with_media(group):
    core, transport, clock, client = group
    assert core.connection_status(client)['routing'] == {
        'delivery_filter': 'all', 'capture_context': False, 'accept_media': True,
        'wake_interval': 0, 'wake_exempt': 'addressed,owner', 'owner_notify': False}
    append(transport, 'plain owner message')
    append(transport, 'stranger message', sender=303)
    core.collect()
    assert [d['text'] for d in deliveries(core, client)] == ['plain owner message']
    assert rows(core, 'SELECT count(*) FROM inbox WHERE context=1') == [(0,)]
    assert rows(core, "SELECT reason FROM unmatched WHERE reason='sender_not_allowed'") == [('sender_not_allowed',)]
    media(transport, caption='a file', document={'file_id': 'D', 'file_unique_id': 'uD'})
    core.collect()
    [delivery] = deliveries(core, client)
    assert delivery['text'] == 'a file' and delivery['attachments'][0]['file_unique_id'] == 'uD'


def test_refused_media_from_an_allowed_sender_is_announced_not_dropped(group):
    core, transport, clock, client = group
    core.delivery_settings(client, accept_media=False)
    media(transport, caption='look at this', photo=[{'file_id': 'P', 'file_unique_id': 'uP', 'file_size': 5}])
    media(transport, document={'file_id': 'D', 'file_unique_id': 'uD'})
    media(transport, sender=303, caption='stranger file', document={'file_id': 'S', 'file_unique_id': 'uS'})
    core.collect()
    got = deliveries(core, client)
    assert [(d['text'], d.get('media_refused'), 'attachments' in d) for d in got] == [
        ('look at this', ['photo'], False), ('', ['document'], False)]
    with pytest.raises(CoreError, match='unknown_attachment'):
        core.fetch_attachment(client, got[0]['delivery_id'], 'uP')
    assert rows(core, "SELECT reason FROM unmatched") == [('unsupported_message',)]


def test_reply_carries_the_quoted_message_and_its_media(group):
    core, transport, clock, client = group
    transport.files['Q'] = {'content': b'quoted image'}
    quoted = {'message_id': 59, 'from': {'id': 303, 'is_bot': False, 'username': 'someone'},
              'chat': {'id': -100, 'type': 'supergroup'}, 'caption': 'the screenshot',
              'photo': [{'file_id': 'Q', 'file_unique_id': 'uQ', 'file_size': 12}]}
    append(transport, 'here it is', reply_to_message=quoted)
    core.collect()
    [delivery] = deliveries(core, client)
    assert delivery['reply_to'] == 59
    assert delivery['reply_to_message'] == {
        'message_id': 59, 'text': 'the screenshot',
        'sender': {'id': 303, 'is_bot': False, 'username': 'someone', 'allowed': False},
        'attachments': [{'kind': 'photo', 'file_id': 'Q', 'file_unique_id': 'uQ', 'file_size': 12}]}
    assert core.fetch_attachment(client, delivery['delivery_id'], 'uQ')['size'] == 12


def test_quoted_media_follows_the_media_setting(group):
    core, transport, clock, client = group
    core.delivery_settings(client, accept_media=False)
    quoted = {'message_id': 59, 'from': {'id': 101, 'is_bot': False},
              'photo': [{'file_id': 'Q', 'file_unique_id': 'uQ'}]}
    append(transport, 'see above', reply_to_message=quoted)
    append(transport, 'sticker reply', reply_to_message={'message_id': 60, 'sticker': {'file_id': 'S'}})
    core.collect()
    first, second = deliveries(core, client)
    assert first['reply_to_message']['media_refused'] == ['photo']
    assert 'attachments' not in first['reply_to_message']
    assert 'reply_to_message' not in second and second['reply_to'] == 60


@pytest.mark.parametrize('origin,expected', [
    ({'type': 'user', 'date': 5, 'sender_user': {'id': 7, 'is_bot': False, 'first_name': 'Ann', 'last_name': 'Lee'}},
     {'type': 'user', 'id': 7, 'name': 'Ann Lee', 'date': 5}),
    ({'type': 'hidden_user', 'date': 5, 'sender_user_name': 'Private Person'},
     {'type': 'hidden_user', 'name': 'Private Person', 'date': 5}),
    ({'type': 'channel', 'date': 5, 'chat': {'id': -1001, 'type': 'channel', 'title': 'News'}, 'message_id': 3},
     {'type': 'channel', 'id': -1001, 'name': 'News', 'date': 5}),
    ({'type': 'unknown_kind'}, None),
])
def test_forwarded_messages_say_where_they_came_from(group, origin, expected):
    core, transport, clock, client = group
    append(transport, 'forwarded text', forward_origin=origin)
    core.collect()
    [delivery] = deliveries(core, client)
    assert delivery['text'] == 'forwarded text'
    assert delivery.get('forwarded_from') == expected


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
    for n in range(core.CONVERSATION_SEND_LIMIT):
        sent = core.send(client, 'reply', f'chain-{n}', reply_to=event['message']['message_id'])
        assert sent['conversation_id'] == f'bot:{client}:202'
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
        db.execute('ALTER TABLE bindings DROP COLUMN accept_media')
        db.execute('DROP INDEX inbox_context_expiry')
        db.execute('ALTER TABLE inbox DROP COLUMN context')
    core = Core(core.state_dir, transport, clock=clock)
    assert core.connection_status(client)['routing'] == {
        'delivery_filter': 'all', 'capture_context': False, 'accept_media': False,
        'wake_interval': 0, 'wake_exempt': 'addressed,owner', 'owner_notify': False}
    assert [d['text'] for d in deliveries(core, client)] == ['before upgrade']


def test_delivery_settings_are_validated_and_operator_only(group):
    core, transport, clock, client = group
    for bad in ({'delivery_filter': 'mentions'}, {'capture_context': 'on'}, {'accept_media': 1}):
        with pytest.raises(CoreError, match='invalid_delivery_settings'):
            core.delivery_settings(client, **bad)
    assert 'delivery_settings' not in {tool.name for tool in TOOLS}
    args = parser().parse_args(['delivery-settings', '--client-file', 'c.json', '--filter', 'addressed',
                                '--context', 'on', '--media', 'on'])
    assert (args.delivery_filter, args.capture_context, args.accept_media) == ('addressed', 'on', 'on')
    unchanged = parser().parse_args(['delivery-settings', '--client-file', 'c.json'])
    assert (unchanged.delivery_filter, unchanged.capture_context, unchanged.accept_media) == (None, None, None)


def media(transport, *, caption=None, sender=101, **fields):
    number = max((u['update_id'] for u in transport.updates), default=0) + 1
    message = {'message_id': number, 'chat': {'id': -100, 'type': 'supergroup', 'title': 'Test group'},
               'from': {'id': sender, 'is_bot': False, 'username': 'participant'}, **fields}
    if caption is not None:
        message['caption'] = caption
    transport.updates.append({'update_id': number, 'message': message})


def test_media_messages_carry_caption_and_attachment_metadata(group):
    core, transport, clock, client = group
    core.delivery_settings(client, accept_media=True)
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
    core.delivery_settings(client, accept_media=True)
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
    core.delivery_settings(client, delivery_filter='addressed', accept_media=True)
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


def test_files_from_senders_that_are_not_allowed_are_never_fetched(group):
    core, transport, clock, client = group
    core.delivery_settings(client, capture_context=True, accept_media=True)
    transport.files['X'] = {'content': b'payload'}
    media(transport, sender=303, caption='take this', document={'file_id': 'X', 'file_unique_id': 'uX'})
    core.collect()
    entry = next(m for m in history(core, client) if m['status'] == 'context')
    assert entry['context_reason'] == 'sender_not_allowed' and entry['attachments'][0]['file_unique_id'] == 'uX'
    with pytest.raises(CoreError, match='attachment_sender_not_allowed'):
        core.fetch_attachment(client, entry['delivery_id'], 'uX')
    assert not (core.state_dir / 'attachments').exists()


def test_pruning_deletes_fetched_files_and_never_the_newest_row(group):
    core, transport, clock, client = group
    core.delivery_settings(client, delivery_filter='addressed', accept_media=True)
    transport.files['C'] = {'content': b'context file'}
    media(transport, caption='aside', date=int(clock.now), document={'file_id': 'C', 'file_unique_id': 'uC'})
    core.collect()
    entry = next(m for m in history(core, client) if m['status'] == 'context')
    path = core.fetch_attachment(client, entry['delivery_id'], 'uC')['path']
    clock.advance(core.HISTORY_WINDOW + 10)
    core.collect()
    # The expired row is the newest inbox row, so it stays until something newer arrives.
    assert os.path.exists(path)
    assert rows(core, 'SELECT count(*) FROM inbox WHERE context=1') == [(1,)]
    append(transport, 'later chatter', date=int(clock.now))
    core.collect()
    assert not os.path.exists(path)
    assert rows(core, 'SELECT id FROM inbox WHERE id=?', (entry['delivery_id'],)) == []


def test_pruning_uses_the_partial_context_index(group):
    core, transport, clock, client = group
    plan = rows(core, """EXPLAIN QUERY PLAN SELECT id FROM inbox WHERE context=1 AND COALESCE(event_at,created)<?
        AND rowid<(SELECT MAX(rowid) FROM inbox)""", (0,))
    assert any('inbox_context_expiry' in str(step) for step in plan)


def test_quoted_text_is_bounded_and_marks_allowed_senders(group):
    core, transport, clock, client = group
    long_text = 'x' * 4096
    append(transport, 'replying to myself', reply_to_message={
        'message_id': 7, 'from': {'id': 101, 'is_bot': False}, 'text': long_text})
    core.collect()
    [delivery] = deliveries(core, client)
    quoted = delivery['reply_to_message']
    assert len(quoted['text']) == 1024 and quoted['truncated'] is True
    assert quoted['sender']['allowed'] is True


def test_forum_topic_root_is_not_quoted(group):
    core, transport, clock, client = group
    # In a forum, Telegram sets reply_to_message to the topic's creation message.
    append(transport, 'in a topic', reply_to_message={
        'message_id': 3, 'from': {'id': 101, 'is_bot': False},
        'forum_topic_created': {'name': 'Topic', 'icon_color': 1}})
    core.collect()
    [delivery] = deliveries(core, client)
    assert 'reply_to_message' not in delivery and delivery['reply_to'] == 3


def test_a_full_page_of_long_quoted_replies_fits_the_result_cap(group):
    from agents_over_telegram.mcp_server import _result_body
    core, transport, clock, client = group
    wide = '\U0001F600' * 4096
    for _ in range(50):
        append(transport, wide, reply_to_message={'message_id': 7, 'from': {'id': 303, 'is_bot': False}, 'text': wide})
    core.collect()
    body = _result_body(core.poll(client, limit=50))
    assert body['ok'] is True and len(body['result']['deliveries']) == 50
