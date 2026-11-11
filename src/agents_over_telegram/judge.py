"""Optional model-judged wake decision for addressed-only delivery.

With ``--filter addressed``, the rules (a mention, a reply to the bot, a
command, a person's quick follow-up) keep every other message as context. When
the owner configures a decision model and turns ``--model-judge on`` for a
connection, AOT asks that model whether such a message is meant for this
agent. A "yes" turns the context entry into a delivery; anything else,
including an error or a timeout, keeps it as context.

The model is any endpoint that speaks TypeSafe's System One decision API:
Jev, Cloudflare's Clef and Clef-flash on Workers AI, or a self-hosted Clef. The
request is one ``choice`` question with the options ``yes`` and ``no``; the
answer is a probability per option, not generated text.

The endpoint, the model and the optional key live only in a protected file in
the state directory, like the bot token. They never enter MCP arguments, tool
results or diagnostics.
"""
from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .credentials import CredentialError, read_private_json, write_private_json
from .net import RacingHTTPSHandler

JUDGE_FILE = 'judge.json'
TIMEOUT_SECONDS = 8.0
TEXT_LIMIT = 600            # characters kept from each message sent to the model
RESPONSE_LIMIT = 1_000_000  # bytes read from a response
DEFAULT_THRESHOLD = 0.5
QUESTION = 'addressed'

INSTRUCTIONS = (
    "This is a Telegram group where people talk with each other and with AI agents. All agents "
    "post through shared bots, and each agent's message starts with a label such as "
    "\"[Agent: Name | shared bot @bot]\". A person usually reaches an agent by mentioning its "
    "bot or replying to it, but sometimes answers it without doing either, for example after a "
    "pause. Is the new message meant for the agent named in the state: does it answer or continue "
    "something that agent said or asked, or ask that agent to do something?"
)
CRITERIA = {
    'yes': 'The new message is meant for this agent.',
    'no': 'The new message is meant for someone else, is general chat, or it is unclear.',
}


class JudgeUnavailable(Exception):
    """The judge gave no usable answer; the message stays context."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A redirect could carry the key to another host; never follow one.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _loopback(host):
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_endpoint(endpoint):
    """An https URL, or plain http only to this computer (a self-hosted model)."""
    if not isinstance(endpoint, str) or not 0 < len(endpoint) <= 2048 or any(c.isspace() for c in endpoint):
        raise CredentialError('invalid_judge_endpoint')
    parts = urllib.parse.urlsplit(endpoint)
    if parts.username or parts.password or parts.fragment or not parts.hostname:
        raise CredentialError('invalid_judge_endpoint')
    if parts.scheme != 'https' and not (parts.scheme == 'http' and _loopback(parts.hostname)):
        raise CredentialError('judge_endpoint_needs_https')
    return endpoint


def check_model(model):
    if not isinstance(model, str) or not 0 < len(model.strip()) <= 128 or any(c.isspace() for c in model.strip()):
        raise CredentialError('invalid_judge_model')
    return model.strip()


def check_threshold(threshold):
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0.5 <= threshold < 1:
        raise CredentialError('invalid_judge_threshold')
    return float(threshold)


def judge_path(state_dir):
    return Path(state_dir) / JUDGE_FILE


def store_config(state_dir, endpoint, model, api_key='', threshold=DEFAULT_THRESHOLD):
    """Write the judge configuration to the protected file, replacing an earlier one."""
    api_key = api_key.strip() if isinstance(api_key, str) else ''
    if len(api_key) > 4096 or any(c.isspace() for c in api_key):
        raise CredentialError('invalid_judge_key')
    write_private_json(judge_path(state_dir), {
        'endpoint': check_endpoint(endpoint), 'model': check_model(model),
        'api_key': api_key, 'threshold': check_threshold(threshold)}, overwrite=True)


def clear_config(state_dir):
    path = judge_path(state_dir)
    if path.exists() or path.is_symlink():
        read_private_json(path)  # refuse to delete a file this user does not own privately
        path.unlink()
        return True
    return False


def config_status(state_dir):
    """What is configured; never the key itself."""
    path = judge_path(state_dir)
    if not path.exists():
        return {'configured': False}
    try:
        judge = _from_file(path)
    except CredentialError as error:
        return {'configured': False, 'error': error.code}
    return {'configured': True, 'endpoint': judge.endpoint, 'model': judge.model,
            'key': bool(judge._api_key), 'threshold': judge.threshold}


def _from_file(path):
    value = read_private_json(path)
    api_key = value.get('api_key', '')
    if not isinstance(api_key, str):
        raise CredentialError('invalid_judge_key')
    return DecisionJudge(check_endpoint(value.get('endpoint')), check_model(value.get('model')),
                         api_key, check_threshold(value.get('threshold', DEFAULT_THRESHOLD)))


def load(state_dir):
    """The judge for this state directory, or None when none is usable."""
    path = judge_path(state_dir)
    if not path.exists():
        return None
    try:
        return _from_file(path)
    except CredentialError:
        return None


def render(agent_label, bot_username, history, candidate):
    """The state: the agent, recent messages oldest first, then the message to judge."""
    def line(entry):
        who = entry.get('sender') or 'unknown'
        text = ' '.join(str(entry.get('text') or '').split())[:TEXT_LIMIT]
        return f'- {who}: {text}'

    recent = '\n'.join(line(entry) for entry in history) or '(no earlier messages)'
    return (f'Agent: "{agent_label}", posting through @{bot_username}.\n\n'
            f'Recent messages, oldest first:\n{recent}\n\n'
            f'New message:\n{line(candidate)}')


class DecisionJudge:
    """One yes/no choice question to a System One decision endpoint."""

    def __init__(self, endpoint, model, api_key='', threshold=DEFAULT_THRESHOLD, *, opener=None):
        self.endpoint, self.model, self.threshold = endpoint, model, threshold
        self._api_key = api_key
        # No proxies from the environment and no redirects, like the Telegram transport.
        self._opener = opener or urllib.request.build_opener(
            _NoRedirect(), urllib.request.ProxyHandler({}), RacingHTTPSHandler())

    def __repr__(self):  # never print the key
        return f'DecisionJudge(endpoint={self.endpoint!r}, model={self.model!r})'

    def request_body(self, *, agent_label, bot_username, history, candidate):
        return {'model': self.model,
                'state': render(agent_label, bot_username, history, candidate),
                'questions': {QUESTION: {'type': 'choice', 'instructions': INSTRUCTIONS, 'criteria': CRITERIA}}}

    def __call__(self, **context):
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
        if self._api_key:
            headers['Authorization'] = 'Bearer ' + self._api_key
        request = urllib.request.Request(self.endpoint, data=json.dumps(self.request_body(**context)).encode(),
                                         headers=headers, method='POST')
        try:
            with self._opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                raw = response.read(RESPONSE_LIMIT + 1)
        except urllib.error.HTTPError as error:
            code = error.code
            if code in (401, 403):
                raise JudgeUnavailable('judge_key_rejected') from None
            if code == 429:
                raise JudgeUnavailable('judge_rate_limited') from None
            if 300 <= code < 400:
                raise JudgeUnavailable('judge_redirect_refused') from None
            if 400 <= code < 500:
                raise JudgeUnavailable('judge_request_rejected') from None
            raise JudgeUnavailable('judge_api_error') from None
        except (urllib.error.URLError, OSError):  # includes timeouts and refused connections
            raise JudgeUnavailable('judge_unreachable') from None
        return self._decide(raw)

    def _decide(self, raw):
        if len(raw) > RESPONSE_LIMIT:
            raise JudgeUnavailable('judge_no_answer')
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            raise JudgeUnavailable('judge_no_answer') from None
        # Cloudflare's REST API may wrap the model's answer in its envelope.
        if isinstance(body, dict) and 'answers' not in body and isinstance(body.get('result'), dict):
            body = body['result']
        answer = body.get('answers', {}).get(QUESTION) if isinstance(body, dict) and isinstance(body.get('answers'), dict) else None
        if not isinstance(answer, dict):
            raise JudgeUnavailable('judge_no_answer')
        probabilities = answer.get('probabilities')
        if isinstance(probabilities, dict):
            yes = probabilities.get('yes')
            if isinstance(yes, (int, float)) and not isinstance(yes, bool) and 0 <= yes <= 1:
                return yes >= self.threshold
        if answer.get('choice') in CRITERIA and self.threshold == DEFAULT_THRESHOLD:
            return answer['choice'] == 'yes'
        raise JudgeUnavailable('judge_no_answer')
