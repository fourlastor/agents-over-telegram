"""send_at: a message or owner notice queued now and sent later by the collector."""
import pytest

from agents_over_telegram.core import CoreError, MAX_SEND_AHEAD, SCHEDULED_ACTION
from agents_over_telegram.mcp_server import _error_body
from test_owner_notify import GROUP, bind, env, owner_messages  # noqa: F401  (env is a fixture)

# The test clock starts at Unix time 1000: 1970-01-01T00:16:40Z.
LATER = '1970-01-01T02:16:40+01:00'  # one hour after the clock start, written as UTC+1
LATER_TIME = 1000.0 + 3600


def group_messages(transport):
    return [m for m in transport.sent if m['chat']['id'] == GROUP]


def test_owner_notice_waits_for_send_at_and_then_goes_out(env):
    core, transport, clock = env
    client = bind(core, transport)
    result = core.owner_notify(client, 'Morning check', 'later-1', choices=['Yes', 'No'], send_at=LATER)
    assert result['status'] == 'queued' and result['error'] is None
    assert result['retry_at'] == LATER_TIME and result['next_action'] == SCHEDULED_ACTION
    # The buttons live for 24 hours from the send time, not from the queue time.
    assert result['choice_expires_at'] == LATER_TIME + core.OWNER_CHOICE_SECONDS
    core.flush()
    assert owner_messages(transport) == []
    clock.now = LATER_TIME
    core.flush()
    [sent] = owner_messages(transport)
    assert 'Morning check' in sent['text']
    status = core.delivery_status(client, result['notify_id'])
    assert status['status'] == 'sent' and 'next_action' not in status


def test_group_message_waits_for_send_at(env):
    core, transport, clock = env
    client = bind(core, transport)
    before = len(group_messages(transport))
    result = core.send(client, 'Reminder', 'later-2', send_at='1970-01-01T01:16:40Z')
    assert result['status'] == 'queued' and result['retry_at'] == LATER_TIME
    assert result['next_action'] == SCHEDULED_ACTION
    clock.now = LATER_TIME - 1
    core.flush()
    assert len(group_messages(transport)) == before
    clock.now = LATER_TIME
    core.flush()
    assert group_messages(transport)[-1]['text'].endswith('\nReminder')
    assert core.delivery_status(client, result['outbox_id'])['status'] == 'sent'


def test_a_time_in_the_past_sends_at_once(env):
    core, transport, _ = env
    client = bind(core, transport)
    result = core.owner_notify(client, 'Now', 'past', send_at='1970-01-01T00:00:00Z')
    assert result['status'] == 'sent'


def test_the_same_key_and_time_returns_the_same_send(env):
    core, transport, _ = env
    client = bind(core, transport)
    first = core.owner_notify(client, 'Once', 'same', send_at=LATER)
    assert core.owner_notify(client, 'Once', 'same', send_at=LATER) == first
    # Another time is another request under the same key.
    with pytest.raises(CoreError) as error:
        core.owner_notify(client, 'Once', 'same', send_at='1970-01-01T02:00:00Z')
    assert error.value.code == 'idempotency_conflict'
    # Without send_at the old hash still applies, so earlier keys keep matching.
    sent = core.owner_notify(client, 'Plain', 'plain')
    assert core.owner_notify(client, 'Plain', 'plain') == sent


@pytest.mark.parametrize('value, code', [
    ('1970-01-01T01:00:00', 'invalid_send_at'),  # no UTC offset
    ('tomorrow at nine', 'invalid_send_at'),
    (3600, 'invalid_send_at'),
    ('1970-01-09T00:16:41Z', 'send_at_too_far'),
])
def test_bad_send_at_values_are_refused_and_nothing_is_queued(env, value, code):
    core, transport, _ = env
    client = bind(core, transport)
    with pytest.raises(CoreError) as error:
        core.owner_notify(client, 'x', 'bad', send_at=value)
    assert error.value.code == code
    with pytest.raises(CoreError) as error:
        core.send(client, 'x', 'bad', send_at=value)
    assert error.value.code == code
    # The key was never stored, so a good request can still use it.
    assert core.owner_notify(client, 'x', 'bad')['status'] == 'sent'


def test_seven_days_ahead_is_allowed(env):
    core, transport, _ = env
    client = bind(core, transport)
    result = core.owner_notify(client, 'Next week', 'week', send_at='1970-01-08T00:16:40Z')
    assert result['retry_at'] == 1000.0 + MAX_SEND_AHEAD


def test_send_at_with_a_peer_recipient_is_refused(env):
    core, transport, _ = env
    client = bind(core, transport)
    with pytest.raises(CoreError) as error:
        core.send(client, 'x', 'peer', recipient='someone', send_at=LATER)
    assert error.value.code == 'send_at_with_recipient'


def test_error_messages_explain_send_at():
    for code in ('invalid_send_at', 'send_at_too_far', 'send_at_with_recipient'):
        assert 'send_at' in _error_body(code)['error']['message']
