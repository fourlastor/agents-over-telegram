#!/usr/bin/env python3
"""A deterministic OpenAI-compatible chat model for host e2e tests.

It lets a real agent host (for example Pi) run its real tool loop, steering and
MCP tools when the host cannot reach a real model. Only the model's choices are
scripted:

* When an "[Agents over Telegram]" notice is in the conversation and not handled
  yet, it calls messages_poll, then message_send ("ACK <first upper-case word of
  the Telegram text>"), then messages_ack. Telegram work goes first.
* If the first user prompt mentions "sleep", it runs `sleep 15` three times with
  the bash tool, one call per turn, then answers "All done."

Every request is appended to --log as one JSON line for evidence.
Usage: scripted_model.py --port 18766 --log FILE
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import time

NOTICE = '[Agents over Telegram]'


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ''.join(part.get('text', '') for part in content if isinstance(part, dict))
    return ''


def _tool(tools, suffix):
    for tool in tools or []:
        name = tool.get('function', {}).get('name', '')
        if name == suffix or name.endswith(suffix):
            return name
    return None


def decide(body: dict) -> dict:
    """Return {'tool': (name, args)} or {'text': str} for the next assistant step."""
    messages = body.get('messages', [])
    tools = body.get('tools', [])
    poll, send, ack, bash = (_tool(tools, s) for s in ('messages_poll', 'message_send', 'messages_ack', 'bash'))
    # Index of the newest notice that is still unhandled.
    notice_at = max((i for i, m in enumerate(messages)
                     if m.get('role') in ('user', 'system') and NOTICE in _text(m.get('content'))), default=None)
    calls_after = []
    results = {}
    for m in messages[(notice_at or 0):]:
        for call in m.get('tool_calls') or []:
            calls_after.append(call['function']['name'])
        if m.get('role') == 'tool':
            results[m.get('tool_call_id')] = _text(m.get('content'))
    if notice_at is not None and poll and send and ack:
        poll_result = None
        for m in messages[notice_at:]:
            for call in m.get('tool_calls') or []:
                if call['function']['name'] == poll:
                    poll_result = results.get(call['id'])
        if poll not in calls_after:
            return {'tool': (poll, {})}
        if poll_result is not None:
            ids = re.findall(r'"delivery_id"\s*:\s*"([^"]+)"', poll_result)
            lease = re.search(r'"lease_token"\s*:\s*"([^"]+)"', poll_result)
            texts = re.findall(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"', poll_result)
            if ids and lease:
                if send not in calls_after:
                    word = next(iter(re.findall(r'\b[A-Z]{3,}\b', ' '.join(texts))), 'OK')
                    return {'tool': (send, {'text': f'ACK {word}', 'idempotency_key': f'scripted-{ids[0]}'})}
                if ack not in calls_after:
                    return {'tool': (ack, {'delivery_ids': ids, 'lease_token': lease.group(1)})}
    first_user = next((_text(m.get('content')) for m in messages if m.get('role') == 'user'
                       and NOTICE not in _text(m.get('content'))), '')
    if 'sleep' in first_user and bash:
        sleeps = sum(1 for m in messages for c in (m.get('tool_calls') or [])
                     if c['function']['name'] == bash and 'sleep' in c['function'].get('arguments', ''))
        if sleeps < 3:
            return {'tool': (bash, {'command': 'sleep 15'})}
    return {'text': 'All done.'}


def _chunk(delta, finish=None):
    return {'id': 'chatcmpl-scripted', 'object': 'chat.completion.chunk', 'created': int(time.time()),
            'model': 'scripted', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}


class Handler(BaseHTTPRequestHandler):
    log_path = None
    counter = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps({'object': 'list', 'data': [{'id': 'scripted', 'object': 'model'}]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get('Content-Length', '0'))
        body = json.loads(self.rfile.read(length) or b'{}')
        step = decide(body)
        Handler.counter += 1
        with open(Handler.log_path, 'a', encoding='utf-8') as log:
            log.write(json.dumps({'at': time.time(), 'n': Handler.counter, 'step': step,
                                  'messages': body.get('messages', [])[-4:]}) + '\n')
        if 'tool' in step:
            name, args = step['tool']
            call = {'index': 0, 'id': f'call_{Handler.counter}', 'type': 'function',
                    'function': {'name': name, 'arguments': json.dumps(args)}}
            chunks = [_chunk({'role': 'assistant', 'tool_calls': [call]}), _chunk({}, 'tool_calls')]
            message = {'role': 'assistant', 'content': None, 'tool_calls': [call]}
            finish = 'tool_calls'
        else:
            chunks = [_chunk({'role': 'assistant', 'content': step['text']}), _chunk({}, 'stop')]
            message = {'role': 'assistant', 'content': step['text']}
            finish = 'stop'
        if body.get('stream'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            for chunk in chunks:
                self.wfile.write(b'data: ' + json.dumps(chunk).encode() + b'\n\n')
            usage = {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}
            self.wfile.write(b'data: ' + json.dumps({**_chunk({}), 'choices': [], 'usage': usage}).encode() + b'\n\n')
            self.wfile.write(b'data: [DONE]\n\n')
        else:
            out = json.dumps({'id': 'chatcmpl-scripted', 'object': 'chat.completion', 'model': 'scripted',
                              'choices': [{'index': 0, 'message': message, 'finish_reason': finish}],
                              'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(out)))
            self.end_headers()
            self.wfile.write(out)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18766)
    parser.add_argument('--log', required=True)
    args = parser.parse_args()
    Handler.log_path = args.log
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
