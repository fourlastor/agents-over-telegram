"""Telegram HTTP and deterministic fake transport; no network exception text escapes."""
from __future__ import annotations

from contextlib import contextmanager
import copy
import fcntl
import json
import os
from pathlib import Path
import threading
import urllib.error
import urllib.request

from .credentials import load_token, private_directory, read_private_json, validate_token, write_private_json


class TransportError(Exception):
    def __init__(self, code="transport_failure", retry_after=None):
        # Codes are controlled by us; never use Telegram descriptions/URLs here.
        self.code = code if isinstance(code, (int, str)) else "transport_failure"
        self.retry_after = retry_after
        super().__init__(str(self.code))


class UnknownSend(TransportError):
    def __init__(self, code="unknown_send", retry_after=None):
        super().__init__(code, retry_after)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TelegramTransport:
    def __init__(self, token, *, opener=None):
        self._token = validate_token(token)
        self._opener = opener or urllib.request.build_opener(_NoRedirect(), urllib.request.ProxyHandler({}))

    def _call(self, method, params, *, sending=False, timeout=15):
        request = urllib.request.Request(
            "https://api.telegram.org/bot" + self._token + "/" + method,
            data=json.dumps(params).encode(), headers={"Content-Type": "application/json"}, method="POST")
        try:
            try:
                with self._opener.open(request, timeout=timeout) as response:
                    data = response.read(2_000_001)
            except urllib.error.HTTPError as error:
                code = error.code
                # Do not trust response bodies or redirects. Only definite client
                # rejection is retryable; server failure may follow acceptance.
                if code == 429:
                    try:
                        raw = json.loads(error.read(16384))
                        retry = raw.get("parameters", {}).get("retry_after", 30)
                        retry = max(1, min(86400, int(retry)))
                    except Exception:
                        retry = 30
                    raise TransportError(429, retry) from None
                if 400 <= code < 500:
                    raise TransportError(code) from None
                raise UnknownSend() if sending else TransportError("http_failure") from None
            if len(data) > 2_000_000:
                raise ValueError()
            payload = json.loads(data)
            if not isinstance(payload, dict):
                raise ValueError()
            if payload.get("ok") is not True:
                code = payload.get("error_code")
                if type(code) is int and 400 <= code < 500:
                    retry = payload.get("parameters", {}).get("retry_after", 30) if code == 429 else None
                    if retry is not None:
                        retry = max(1, min(86400, int(retry)))
                    raise TransportError(code, retry)
                raise UnknownSend() if sending else TransportError("invalid_response")
            result = payload["result"]
            if sending and (not isinstance(result, dict) or type(result.get("message_id")) is not int):
                raise ValueError()
            return result
        except TransportError:
            raise
        except Exception:
            raise UnknownSend() if sending else TransportError("transport_failure") from None

    def get_me(self):
        result = self._call("getMe", {})
        if not isinstance(result, dict) or type(result.get("id")) is not int or result.get("is_bot") is not True:
            raise TransportError("invalid_bot_identity")
        return result

    def get_webhook_info(self):
        result = self._call("getWebhookInfo", {})
        if not isinstance(result, dict) or not isinstance(result.get("url"), str):
            raise TransportError("invalid_webhook_status")
        return {"url": result["url"]}  # Never exposed as diagnostic output.

    def get_updates(self, offset, timeout=0):
        timeout = max(0, min(20, int(timeout)))
        result = self._call("getUpdates", {"offset": offset, "timeout": timeout, "limit": 100,
            "allowed_updates": ["message", "edited_message", "my_chat_member"]}, timeout=timeout + 15)
        if not isinstance(result, list):
            raise TransportError("invalid_updates")
        return result

    def send_message(self, chat_id, text, thread_id=None, reply_to=None):
        params = {"chat_id": chat_id, "text": text, "link_preview_options": {"is_disabled": True}}
        if thread_id is not None:
            params["message_thread_id"] = thread_id
        if reply_to is not None:
            params["reply_parameters"] = {"message_id": reply_to}
        return self._call("sendMessage", params, sending=True)


class FakeTransport:
    """No sockets; modes sent/reject/rate_limit/unknown/crash model failure windows."""
    def __init__(self, updates=None, *, bot_id=900001, username="aot_test_bot", webhook="", outcomes=None):
        self.updates = list(updates or [])
        self.bot_id = bot_id
        self.username = username
        self.webhook = webhook
        self.outcomes = list(outcomes or [])
        self.sent = []
        self.poll_offsets = []
        self._lock = threading.RLock()

    def get_me(self):
        return {"id": self.bot_id, "username": self.username, "is_bot": True}

    def get_webhook_info(self):
        return {"url": self.webhook}

    def get_updates(self, offset, timeout=0):
        with self._lock:
            self.poll_offsets.append(offset)
            return copy.deepcopy(sorted((u for u in self.updates if u["update_id"] >= offset), key=lambda x:x["update_id"])[:100])

    def send_message(self, chat_id, text, thread_id=None, reply_to=None):
        with self._lock:
            outcome = self.outcomes.pop(0) if self.outcomes else "sent"
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome == "reject":
                raise TransportError(403)
            if outcome == "rate_limit":
                raise TransportError(429, 30)
            message = {"message_id": len(self.sent)+1, "chat": {"id": chat_id}, "text": text,
                       "message_thread_id": thread_id, "reply_to_message_id": reply_to}
            self.sent.append(message)
            if outcome == "unknown":
                raise UnknownSend()
            if outcome == "crash":
                raise SystemExit("fake crash after acceptance")
            return copy.deepcopy(message)


class FileFakeTransport:
    """Explicit offline mode shared by independent test MCP subprocesses."""
    def __init__(self, path):
        self.path = Path(path)
        private_directory(self.path.parent)

    @contextmanager
    def _data(self):
        fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = read_private_json(self.path) if self.path.exists() else {
                "bot_id": 900001, "username": "aot_test_bot", "updates": [], "sent": [], "outcomes": [], "poll_offsets": []}
            yield data
            write_private_json(self.path, data, overwrite=self.path.exists())
        finally:
            os.close(fd)

    def get_me(self):
        with self._data() as data:
            return {"id": data["bot_id"], "username": data["username"], "is_bot": True}

    def get_webhook_info(self):
        with self._data() as data:
            return {"url": data.get("webhook", "")}

    def get_updates(self, offset, timeout=0):
        with self._data() as data:
            data["poll_offsets"].append(offset)
            return sorted([u for u in data["updates"] if u["update_id"] >= offset], key=lambda x:x["update_id"])[:100]

    def send_message(self, chat_id, text, thread_id=None, reply_to=None):
        with self._data() as data:
            outcome = data["outcomes"].pop(0) if data["outcomes"] else "sent"
            result = {"message_id": len(data["sent"])+1, "chat": {"id": chat_id}, "text": text,
                      "message_thread_id": thread_id, "reply_to_message_id": reply_to}
            if outcome not in ("reject", "rate_limit"):
                data["sent"].append(result)
        if outcome == "reject":
            raise TransportError(403)
        if outcome == "rate_limit":
            raise TransportError(429, 30)
        if outcome == "unknown":
            raise UnknownSend()
        if outcome == "crash":
            raise SystemExit("fake crash after acceptance")
        return result


def make_transport(state_dir, *, fake=False):
    return FileFakeTransport(Path(state_dir) / "fake-transport.json") if fake else TelegramTransport(load_token(state_dir))
