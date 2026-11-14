"""Agent Markdown shown formatted in Telegram, and entities read back as Markdown."""
import pytest

from agents_over_telegram.markdown import agent_html, entities_markdown, to_html
from test_core import append, bind, env  # noqa: F401 (env is a fixture)
from test_owner_approval import press
from test_owner_choices import deliveries, tap

HEADER = '[Agent: Alpha | shared bot @aot_test_bot]'


def test_the_supported_subset_becomes_telegram_html():
    text = ('Run `sudo cat /etc/a<b>` now.\n'
            '**Bold**, *italic*, _also italic_ and ~~gone~~.\n'
            '- one [link](https://example.com/a?b=1&c=2)\n'
            '* two\n'
            '## Heading with `code`\n'
            '```python\nif a < b:\n    print("**x**")\n```')
    assert to_html(text) == (
        'Run <code>sudo cat /etc/a&lt;b&gt;</code> now.\n'
        '<b>Bold</b>, <i>italic</i>, <i>also italic</i> and <s>gone</s>.\n'
        '• one <a href="https://example.com/a?b=1&amp;c=2">link</a>\n'
        '• two\n'
        '<b>Heading with <code>code</code></b>\n'
        '<pre><code class="language-python">if a &lt; b:\n    print("**x**")</code></pre>')


@pytest.mark.parametrize('text', [
    'snake_case_name and __init__ stay as written',
    '2 * 3 * 4 is not italic',
    'a ** b is not bold',
    '[not a link](javascript:alert(1))',
    '| a | b |\n|---|---|',
])
def test_text_that_only_looks_like_markdown_is_left_alone(text):
    assert agent_html(f'{HEADER}\n{text}') is None


def test_the_label_line_stays_plain():
    html = agent_html('[Agent: *Star* | <Group>]\nsee `x`')
    assert html == '[Agent: *Star* | &lt;Group&gt;]\nsee <code>x</code>'


def test_text_without_the_label_line_is_never_formatted():
    assert agent_html('AOT notice with `code`') is None


def test_entities_come_back_as_markdown():
    # The emoji is two UTF-16 units, as Telegram counts offsets.
    text = '😀 see docs and run ls -la\nblock'
    entities = [{'type': 'text_link', 'offset': 7, 'length': 4, 'url': 'https://d.example'},
                {'type': 'bold', 'offset': 3, 'length': 3},
                {'type': 'code', 'offset': 20, 'length': 6},
                {'type': 'pre', 'offset': 27, 'length': 5, 'language': 'sh'}]
    assert entities_markdown(text, entities) == '😀 see [docs](https://d.example) and run `ls -la`\n```sh\nblock\n```'


@pytest.mark.parametrize('entities', [
    [{'type': 'code', 'offset': 0, 'length': 99}],
    [{'type': 'code', 'offset': 0, 'length': 3}, {'type': 'code', 'offset': 2, 'length': 2}],
    [{'type': 'code', 'offset': -1, 'length': 2}],
])
def test_malformed_entities_leave_the_text_unchanged(entities):
    assert entities_markdown('abcdef', entities) == 'abcdef'


def test_a_group_send_goes_out_formatted(env):
    core, transport, _ = env
    client, _ = bind(core, transport)
    core.send(client, 'Run `make test`', 'k1')
    message = transport.sent[-1]
    assert message['parse_mode'] == 'HTML'
    assert message['text'].endswith('\nRun <code>make test</code>')


def test_plain_text_goes_out_without_a_parse_mode(env):
    core, transport, _ = env
    client, _ = bind(core, transport)
    core.send(client, 'Nothing to format, 1 < 2', 'k1')
    assert 'parse_mode' not in transport.sent[-1]
    assert transport.sent[-1]['text'].endswith('\nNothing to format, 1 < 2')


def test_formatting_telegram_rejects_falls_back_to_plain_text(env):
    core, transport, _ = env
    client, _ = bind(core, transport)
    transport.outcomes = ['bad_entities']
    result = core.send(client, '**bold**', 'k1')
    assert result['status'] == 'sent'
    assert 'parse_mode' not in transport.sent[-1]
    assert transport.sent[-1]['text'].endswith('\n**bold**')
    assert len([m for m in transport.sent if m['text'].endswith('bold**')]) == 1


def test_an_owner_notice_and_its_answer_keep_their_formatting(env):
    core, transport, _ = env
    client, _ = bind(core, transport)
    core.delivery_settings(client, owner_notify=True)
    status = core.owner_notify(client, 'Deploy **now**?', 'q1', choices=['Yes', 'No'])
    message = transport.sent[-1]
    assert message['parse_mode'] == 'HTML' and 'Deploy <b>now</b>?' in message['text']
    tap(transport, status, 0)
    core.collect()
    deliveries(core, client)
    assert transport.edits[-1]['parse_mode'] == 'HTML'
    assert transport.edits[-1]['text'].endswith('\n\nAnswered: Yes')


def test_approval_requests_and_their_edits_stay_plain(env):
    core, transport, _ = env
    client, _ = bind(core, transport)
    summary = 'See [the docs](https://example.com) and **this**.'
    approval = core.request_owner_approval(client, 'delivery_settings', {'auto_allow': True}, summary)
    assert 'parse_mode' not in transport.sent[-1]
    press(transport, approval['approval_id'])
    core.collect()
    assert 'parse_mode' not in transport.edits[-1]
    assert summary in transport.edits[-1]['text']


def test_incoming_code_and_links_reach_the_agent_as_markdown(env):
    core, transport, _ = env
    client, _ = bind(core, transport)
    append(transport, 'run ls and read docs', entities=[
        {'type': 'code', 'offset': 4, 'length': 2},
        {'type': 'text_link', 'offset': 16, 'length': 4, 'url': 'https://d.example'}])
    core.collect()
    [delivery] = deliveries(core, client)
    assert delivery['text'] == 'run `ls` and read [docs](https://d.example)'
