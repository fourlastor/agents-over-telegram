"""A safe Markdown subset for Telegram, in both directions.

Agents write Markdown. Telegram shows it literally unless the message is
sent with a parse mode. ``agent_html`` turns the text an agent wrote into
Telegram HTML. It escapes everything, then adds tags only for a small subset:
inline code, fenced code blocks, bold, italic, strikethrough, http(s) links,
headings (as bold) and bullets. Anything else stays as written.

``entities_markdown`` goes the other way for incoming messages. It turns
code, pre and text_link entities back into Markdown, so an agent still sees
code spans and link targets that Telegram removed from the text.
"""

from __future__ import annotations

import html
import re

# Agent text always starts with this label line. It stays plain, so a label
# or a group title can never become formatting.
_HEADER = re.compile(r'\[Agent: [^\n]*\]')
_FENCE = re.compile(r'^```([A-Za-z0-9_+-]{0,32})[ \t]*\n(.*?)\n?^```[ \t]*$', re.MULTILINE | re.DOTALL)
_CODE = re.compile(r'`([^`\n]+)`')
_LINK = re.compile(r'\[([^\[\]\n]+)\]\((https?://[^\s()<>]+)\)')
# Only ** makes bold: __name__ is far more often a Python name than emphasis.
_BOLD = re.compile(r'(?<![\w*])\*\*(?=\S)(.+?)(?<=\S)\*\*(?![\w*])')
_ITALIC = re.compile(r'(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])'
                     r'|(?<![\w_])_(?=[^\s_])([^_\n]+?)(?<=[^\s_])_(?![\w_])')
_STRIKE = re.compile(r'(?<!~)~~(?=\S)([^~\n]+?)(?<=\S)~~(?!~)')
_HEADING = re.compile(r'^#{1,6}[ \t]+(.+?)[ \t#]*$', re.MULTILINE)
_BULLET = re.compile(r'^([ \t]*)[-*+][ \t]+', re.MULTILINE)
# Placeholders for finished pieces; NUL never survives into Telegram text.
_SLOT = re.compile('\x00(\\d+)\x00')


def agent_html(text):
    """Telegram HTML for agent text that starts with its label line, or None.

    None means: send the text as it is. Only agent text carries the label
    line, so AOT's own notices are never reformatted.
    """
    if not isinstance(text, str):
        return None
    header, newline, body = text.partition('\n')
    if not _HEADER.fullmatch(header):
        return None
    rendered = to_html(body)
    if rendered == html.escape(body, quote=False):
        return None  # Nothing to format; plain text is the safer send.
    return html.escape(header, quote=False) + newline + rendered


def to_html(text):
    """Telegram HTML for one piece of Markdown; every other character is escaped."""
    pieces = []

    def keep(markup):
        pieces.append(markup)
        return f'\x00{len(pieces) - 1}\x00'

    def fence(match):
        language, code = match.group(1), html.escape(match.group(2), quote=False)
        if language:
            return keep(f'<pre><code class="language-{language}">{code}</code></pre>')
        return keep(f'<pre>{code}</pre>')

    text = text.replace('\x00', '')
    text = _FENCE.sub(fence, text)
    text = _CODE.sub(lambda m: keep(f'<code>{html.escape(m.group(1), quote=False)}</code>'), text)
    text = _LINK.sub(lambda m: keep(f'<a href="{html.escape(m.group(2))}">{_inline(m.group(1))}</a>'), text)
    text = _HEADING.sub(lambda m: keep(f'<b>{_inline(m.group(1))}</b>'), text)
    text = _BULLET.sub(lambda m: m.group(1) + '• ', text)
    text = _inline(text)
    # A heading or link can hold a code span, so resolve until none are left.
    while _SLOT.search(text):
        text = _SLOT.sub(lambda m: pieces[int(m.group(1))], text)
    return text


def _inline(text):
    """Escape text and add bold, italic and strikethrough; placeholders pass through."""
    text = html.escape(text, quote=False)
    text = _BOLD.sub(lambda m: f'<b>{m.group(1)}</b>', text)
    text = _ITALIC.sub(lambda m: f'<i>{m.group(1) or m.group(2)}</i>', text)
    return _STRIKE.sub(lambda m: f'<s>{m.group(1)}</s>', text)


def entities_markdown(text, entities):
    """Text with code, pre and text_link entities written back as Markdown.

    Other entities (bold, mentions, plain URLs) keep their text as it is.
    Offsets are in UTF-16 code units, as Telegram counts them. Overlapping
    or malformed entities leave the text unchanged.
    """
    if not isinstance(text, str) or not isinstance(entities, list) or not entities:
        return text
    units = text.encode('utf-16-le')
    spans = []
    for entity in entities:
        if not isinstance(entity, dict) or entity.get('type') not in ('code', 'pre', 'text_link'):
            continue
        offset, length = entity.get('offset'), entity.get('length')
        if type(offset) is not int or type(length) is not int or offset < 0 or length <= 0:
            return text
        if (offset + length) * 2 > len(units):
            return text
        spans.append((offset, offset + length, entity))
    spans.sort(key=lambda span: span[0])
    if any(a[1] > b[0] for a, b in zip(spans, spans[1:])):
        return text
    out, at = [], 0
    for start, end, entity in spans:
        out.append(units[at * 2:start * 2].decode('utf-16-le', 'replace'))
        inner = units[start * 2:end * 2].decode('utf-16-le', 'replace')
        kind = entity['type']
        if kind == 'code':
            out.append(f'`{inner}`')
        elif kind == 'pre':
            language = entity.get('language') if isinstance(entity.get('language'), str) else ''
            language = language if re.fullmatch(r'[A-Za-z0-9_+-]{0,32}', language) else ''
            out.append(f'```{language}\n{inner}\n```')
        else:
            url = entity.get('url')
            out.append(f'[{inner}]({url})' if isinstance(url, str) and not re.search(r'\s', url) else inner)
        at = end
    out.append(units[at * 2:].decode('utf-16-le', 'replace'))
    return ''.join(out)
