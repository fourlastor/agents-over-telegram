"""Grok Bot host: the connector from setup, a piped token, and webhook wakeups."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import threading
import time

import pytest

from agents_over_telegram import cli, webhook
from agents_over_telegram.credentials import CredentialError, read_private_json
from conftest import posix_only
from test_hosts import RUNTIME, clean_env, connected, invoke, prepare, receive


class Hook:
    """A local webhook endpoint that records each request."""

    def __init__(self, statuses=()):
        self.requests = []
        self.statuses = list(statuses)
        hook = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get('Content-Length', 0))
                hook.requests.append({'auth': self.headers.get('Authorization'),
                                      'body': json.loads(self.rfile.read(length)), 'at': time.monotonic()})
                status = hook.statuses.pop(0) if hook.statuses else 200
                self.send_response(status)
                if status in (301, 302, 307):
                    self.send_header('Location', 'http://127.0.0.1:9/elsewhere')
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'{}')

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_port}/routine'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def wait_for(self, count, timeout=10):
        deadline = time.monotonic() + timeout
        while len(self.requests) < count and time.monotonic() < deadline:
            time.sleep(0.02)
        return self.requests

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def hook():
    endpoint = Hook()
    yield endpoint
    endpoint.close()


EVENT = {'type': 'telegram_messages', 'new': 2, 'pending': 3, 'delivery_ids': ['d1', 'd2'],
         'senders': ['@secret_sender'], 'notice': 'notice'}


# Setup

def test_setup_prints_the_command_connector_and_writes_no_host_config(tmp_path):
    result = prepare(tmp_path, 'grok-bot')
    connector = result['connector']
    assert connector['type'] == 'Command' and connector['command'] == str(RUNTIME)
    assert connector['args'][-3:] == ['mcp', '--client-file', result['client_file']]
    assert result['config_file'] is None and result['host'] == 'grok-bot'
    project = Path(result['project_dir'])
    assert not (project / '.codex').exists() and not (project / '.mcp.json').exists()


def test_connected_setup_names_the_connector_and_the_routine(tmp_path):
    connected(tmp_path, host='grok-bot')
    result = prepare(tmp_path, 'grok-bot')
    assert result['connected'] and 'custom connector' in result['next_step'] and 'webhook' in result['next_step']


# A piped token

class Stdin(io.StringIO):
    def __init__(self, text, tty=False):
        super().__init__(text)
        self._tty = tty

    def isatty(self):
        return self._tty


def test_a_piped_token_is_read_without_a_terminal(monkeypatch):
    monkeypatch.setattr(cli.sys, 'stdin', Stdin('123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi\n'))
    assert cli._piped_token() == '123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi'


@pytest.mark.parametrize('stdin, code', [
    (Stdin('123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi\n', tty=True), 'token_stdin_must_be_piped'),
    (Stdin(''), 'invalid_token'),
    (Stdin('one\ntwo\n'), 'invalid_token'),
    (None, 'token_stdin_must_be_piped'),
], ids=['terminal', 'empty', 'two-lines', 'no-stdin'])
def test_a_piped_token_refuses_a_terminal_and_bad_input(monkeypatch, stdin, code):
    monkeypatch.setattr(cli.sys, 'stdin', stdin)
    with pytest.raises(CredentialError) as raised:
        cli._piped_token()
    assert raised.value.args[0] == code


def test_register_with_a_piped_token_validates_it_with_telegram(tmp_path, monkeypatch):
    from agents_over_telegram.core import Core
    from agents_over_telegram.transport import FakeTransport
    token = '123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi'
    seen = []

    class Transport:
        def __init__(self, value):
            seen.append(value)

        def get_me(self):
            return {'id': 42, 'is_bot': True, 'username': 'grok_aot_bot'}

        def get_webhook_info(self):
            return {'url': ''}

    monkeypatch.setattr(cli, 'TelegramTransport', Transport)
    monkeypatch.setattr(cli.sys, 'stdin', Stdin(token + '\n'))
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    core = Core(state, FakeTransport(), recover_sends=False)
    result = cli.register_account(core, state, False, True, token_stdin=True)
    assert seen == [token]
    assert read_private_json(state / 'credentials.json')['token'] == token
    assert token not in json.dumps(result)


def test_without_the_flag_a_pipe_still_needs_the_terminal(tmp_path, monkeypatch):
    from agents_over_telegram.core import Core
    from agents_over_telegram.transport import FakeTransport
    monkeypatch.setattr(cli.sys, 'stdin', Stdin('123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi\n'))
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    with pytest.raises(CredentialError) as raised:
        cli.register_account(Core(state, FakeTransport(), recover_sends=False), state, False, True)
    assert raised.value.args[0] == 'private_terminal_required'


# The webhook file

@pytest.mark.parametrize('url', ['https://hooks.example/run/abc', 'http://127.0.0.1:8080/x', 'http://localhost/x',
                                 'http://[::1]:9/x'])
def test_https_or_this_computer_is_accepted(url):
    assert webhook.validate_url(url) == url


@pytest.mark.parametrize('url, code', [
    ('http://hooks.example/run', 'webhook_url_must_be_https'),
    ('ftp://hooks.example/run', 'webhook_url_must_be_https'),
    ('https://user:pass@hooks.example/run', 'invalid_webhook_url'),
    ('https:///nohost', 'invalid_webhook_url'),
    ('https://hooks.example/a b', 'invalid_webhook_url'),
    ('', 'invalid_webhook_url'),
])
def test_other_urls_are_refused(url, code):
    with pytest.raises(CredentialError) as raised:
        webhook.validate_url(url)
    assert raised.value.args[0] == code


def test_saved_webhook_is_private_and_never_shown(tmp_path):
    path = tmp_path / 'hooks' / 'grok.json'
    path.parent.mkdir(mode=0o700)
    shown = webhook.save(path, 'https://hooks.example/run/abc', 'k3y-secret')
    assert shown == {'webhook_file': str(path), 'host': 'hooks.example'}
    assert webhook.load(path) == ('https://hooks.example/run/abc', 'k3y-secret')
    if os.name != 'nt':
        assert path.stat().st_mode & 0o077 == 0


def test_webhook_save_reads_url_and_key_from_a_pipe(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'grok.json'
    monkeypatch.setattr(cli.sys, 'stdin', Stdin('https://hooks.example/run/abc\nk3y-secret\n'))
    shown = invoke('webhook-save', '--file', path)
    assert shown['host'] == 'hooks.example' and 'k3y-secret' not in json.dumps(shown)
    monkeypatch.setattr(cli.sys, 'stdin', Stdin('https://hooks.example/run\n', tty=True))
    with pytest.raises(CredentialError) as raised:
        invoke('webhook-save', '--file', path)
    assert raised.value.args[0] == 'webhook_input_must_be_piped'


# Waking

def test_the_body_holds_counts_and_fixed_guidance_only(hook):
    waker = webhook.Waker(hook.url, 'k3y', min_gap=0)
    waker.notify(EVENT)
    [request] = hook.wait_for(1)
    waker.stop(5)
    assert request['auth'] == 'Bearer k3y'
    assert request['body'] == {'event': 'telegram_messages_waiting', 'new': 2, 'pending': 3,
                               'guidance': webhook.GUIDANCE}
    assert 'secret_sender' not in json.dumps(request['body'])


def test_bursts_are_coalesced_and_later_work_still_goes_out(hook):
    waker = webhook.Waker(hook.url, 'k3y', min_gap=0.5)
    waker.notify(EVENT)
    hook.wait_for(1)
    for pending in (4, 5, 6):
        waker.notify({**EVENT, 'pending': pending})
    requests = hook.wait_for(2)
    waker.stop(5)
    assert len(requests) == 2 and requests[1]['body']['pending'] == 6
    assert requests[1]['at'] - requests[0]['at'] >= 0.45


def test_a_failed_post_is_retried(hook):
    hook.statuses = [500, 503]
    logged = []
    waker = webhook.Waker(hook.url, 'k3y', min_gap=0, retry_delays=(0.05, 0.05, 0.05), log=logged.append)
    waker.notify(EVENT)
    requests = hook.wait_for(3)
    waker.stop(5)
    assert len(requests) == 3 and waker.sent == 1
    assert logged and all('http_50' in line for line in logged)
    assert not any(hook.url in line or 'k3y' in line for line in logged)


def test_redirects_are_not_followed(hook):
    hook.statuses = [302, 302, 302, 302]
    logged = []
    waker = webhook.Waker(hook.url, 'k3y', min_gap=0, retry_delays=(0.01, 0.01, 0.01), log=logged.append)
    waker.notify(EVENT)
    hook.wait_for(4)
    waker.stop(5)
    assert waker.sent == 0 and 'waiting for new messages' in logged[-1]
    assert all(r['auth'] == 'Bearer k3y' for r in hook.requests) and len(hook.requests) == 4


def test_stop_sends_what_is_still_wanted(hook):
    waker = webhook.Waker(hook.url, 'k3y', min_gap=60)
    waker.notify(EVENT)
    hook.wait_for(1)
    waker.notify({**EVENT, 'pending': 9})
    waker.stop(5)
    assert [r['body']['pending'] for r in hook.requests] == [3, 9]


def test_other_watch_events_do_not_wake(hook):
    waker = webhook.Waker(hook.url, 'k3y', min_gap=0)
    waker.notify({'type': 'watch_stopped', 'reason': 'x'})
    waker.stop(5)
    assert hook.requests == []


@posix_only
def test_watch_posts_to_the_webhook_and_keeps_text_local(tmp_path, hook):
    result = connected(tmp_path, host='grok-bot')
    path = tmp_path / 'grok-hook.json'
    webhook.save(path, hook.url, 'k3y')
    receive(result, 'a private message body')
    done = subprocess.run([str(RUNTIME), 'watch', '--webhook-file', str(path), '--duration', '1',
                           '--interval', '0.1', '--format', 'jsonl'], capture_output=True, text=True,
                          cwd=result['project_dir'], env=clean_env(), timeout=60, stdin=subprocess.DEVNULL)
    assert done.returncode == 0, done.stderr
    [request] = hook.wait_for(1)
    assert request['body']['new'] == 1 and request['auth'] == 'Bearer k3y'
    assert 'private message' not in json.dumps(request['body']) and 'owner_name' not in json.dumps(request['body'])
    assert 'k3y' not in done.stdout + done.stderr and hook.url not in done.stdout + done.stderr
