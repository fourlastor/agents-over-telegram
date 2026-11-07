"""Telegram HTTP and deterministic fake transport; no network exception text escapes."""
from __future__ import annotations

from contextlib import contextmanager
import copy
import fcntl
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
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

    def _call(self, method, params, *, sending=False, timeout=15, expects="message"):
        return self._post(method, json.dumps(params).encode(), "application/json",
                          sending=sending, timeout=timeout, expects=expects)

    def _post(self, method, body, content_type, *, sending=False, timeout=15, expects="message"):
        request = urllib.request.Request(
            "https://api.telegram.org/bot" + self._token + "/" + method,
            data=body, headers={"Content-Type": content_type}, method="POST")
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
            if sending and expects == "message" and (not isinstance(result, dict) or type(result.get("message_id")) is not int):
                raise ValueError()
            # setMessageReaction answers with the bare boolean True.
            if sending and expects == "bool" and result is not True:
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
            # message_reaction must be named explicitly; Telegram never sends it by
            # default, and only while the bot is an administrator in the chat.
            "allowed_updates": ["message", "edited_message", "my_chat_member", "message_reaction"]}, timeout=timeout + 15)
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

    def send_photo(self, chat_id, data, *, caption=None, filename="photo.png",
                   mime="image/png", thread_id=None, reply_to=None):
        """Upload one local image as a photo; caption length is the caller's duty."""
        return self._send_media("sendPhoto", "photo", chat_id, data, caption=caption,
                                filename=filename, mime=mime, thread_id=thread_id, reply_to=reply_to)

    def send_document(self, chat_id, data, *, caption=None, filename="document.bin",
                      mime="application/octet-stream", thread_id=None, reply_to=None):
        """Upload one local file as a document; caption length is the caller's duty."""
        return self._send_media("sendDocument", "document", chat_id, data, caption=caption,
                                filename=filename, mime=mime, thread_id=thread_id, reply_to=reply_to)

    def _send_media(self, method, field, chat_id, data, *, caption, filename, mime, thread_id, reply_to):
        fields = {"chat_id": str(chat_id)}
        if caption:
            fields["caption"] = caption
        if thread_id is not None:
            fields["message_thread_id"] = str(thread_id)
        if reply_to is not None:
            fields["reply_parameters"] = json.dumps({"message_id": reply_to})
        body, content_type = _multipart(fields, [(field, filename, mime, bytes(data))])
        return self._post(method, body, content_type, sending=True)

    def set_message_reaction(self, chat_id, message_id, emoji):
        """Set the bot's one standard-emoji reaction, or remove it when emoji is empty.

        The Bot API answers True; an empty reaction list clears the bot's
        reaction. Anything but a definite True is an uncertain outcome.
        """
        params = {"chat_id": chat_id, "message_id": message_id,
                  "reaction": [{"type": "emoji", "emoji": emoji}] if emoji else []}
        return self._call("setMessageReaction", params, sending=True, expects="bool")

    def get_chat_member(self, chat_id, user_id):
        result = self._call("getChatMember", {"chat_id": chat_id, "user_id": user_id})
        if not isinstance(result, dict) or not isinstance(result.get("status"), str):
            raise TransportError("invalid_chat_member")
        return _chat_member(result)

    def download_file(self, file_id, max_bytes):
        """Resolve a file with getFile, then read at most max_bytes; no redirects."""
        result = self._call("getFile", {"file_id": file_id})
        if not isinstance(result, dict):
            raise TransportError("invalid_file")
        size = result.get("file_size")
        if type(size) is int and size > max_bytes:
            raise TransportError("file_too_large")
        request = urllib.request.Request(
            "https://api.telegram.org/file/bot" + self._token + "/" + _file_path(result.get("file_path")), method="GET")
        try:
            with self._opener.open(request, timeout=60) as response:
                data = response.read(max_bytes + 1)
        except urllib.error.HTTPError as error:
            raise TransportError(error.code if 400 <= error.code < 500 else "http_failure") from None
        except Exception:
            raise TransportError("transport_failure") from None
        if len(data) > max_bytes:
            raise TransportError("file_too_large")
        return data


def _file_path(value):
    """Telegram's relative file path, refused unless it is plainly relative."""
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,256}", value)
            or value.startswith("/") or ".." in value.split("/")):
        raise TransportError("invalid_file")
    return value


def _multipart(fields, files):
    """Encode multipart/form-data with text fields and byte file parts."""
    boundary = "----aot" + secrets.token_hex(16)
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, filename, mime, data in files:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                     f'filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n'.encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


def _fake_file(files, file_id, max_bytes):
    entry = files.get(file_id)
    if not isinstance(entry, dict):
        raise TransportError(400)
    data = base64.b64decode(entry["content_b64"]) if "content_b64" in entry else entry["content"]
    if len(data) > max_bytes:
        raise TransportError("file_too_large")
    return bytes(data)


def _chat_member(value):
    """Keep only the fields the suspension check reads; never user profile data."""
    member = {"status": value.get("status")}
    for key in ("is_member", "can_send_messages"):
        if isinstance(value.get(key), bool):
            member[key] = value[key]
    return member


def _fake_chat_member(members, chat_id, user_id):
    # Telegram answers 400/403 when the bot cannot see the chat. Model that for
    # chats with no explicit entry, so tests fail closed like the real API.
    member = members.get(str(chat_id), {}).get(str(user_id))
    if member is None:
        raise TransportError(403)
    return _chat_member(member)


class FakeTransport:
    """No sockets; modes sent/reject/rate_limit/unknown/crash model failure windows."""
    def __init__(self, updates=None, *, bot_id=900001, username="aot_test_bot", webhook="", outcomes=None):
        self.updates = list(updates or [])
        self.bot_id = bot_id
        self.username = username
        self.webhook = webhook
        self.outcomes = list(outcomes or [])
        self.sent = []
        self.reactions = []
        self.poll_offsets = []
        self.chat_members = {}  # {str(chat_id): {str(user_id): ChatMember dict}}
        self.files = {}  # {file_id: {"content": bytes}}
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

    def send_photo(self, chat_id, data, *, caption=None, filename=None, mime=None,
                   thread_id=None, reply_to=None):
        return self._send_media("photo", chat_id, data, caption, filename, mime, thread_id, reply_to)

    def send_document(self, chat_id, data, *, caption=None, filename=None, mime=None,
                      thread_id=None, reply_to=None):
        return self._send_media("document", chat_id, data, caption, filename, mime, thread_id, reply_to)

    def _send_media(self, kind, chat_id, data, caption, filename, mime, thread_id, reply_to):
        with self._lock:
            outcome = self.outcomes.pop(0) if self.outcomes else "sent"
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome == "reject":
                raise TransportError(403)
            if outcome == "rate_limit":
                raise TransportError(429, 30)
            message = {"message_id": len(self.sent)+1, "chat": {"id": chat_id}, "caption": caption,
                       "message_thread_id": thread_id, "reply_to_message_id": reply_to,
                       "media": {"kind": kind, "data": bytes(data), "filename": filename,
                                 "mime": mime, "sha256": hashlib.sha256(bytes(data)).hexdigest()}}
            self.sent.append(message)
            if outcome == "unknown":
                raise UnknownSend()
            if outcome == "crash":
                raise SystemExit("fake crash after acceptance")
            return copy.deepcopy(message)

    def set_message_reaction(self, chat_id, message_id, emoji):
        with self._lock:
            outcome = self.outcomes.pop(0) if self.outcomes else "sent"
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome == "reject":
                raise TransportError(403)
            if outcome == "rate_limit":
                raise TransportError(429, 30)
            self.reactions.append({"chat_id": chat_id, "message_id": message_id, "emoji": emoji})
            if outcome == "unknown":
                raise UnknownSend()
            if outcome == "crash":
                raise SystemExit("fake crash after acceptance")
            return True

    def get_chat_member(self, chat_id, user_id):
        with self._lock:
            return _fake_chat_member(self.chat_members, chat_id, user_id)

    def download_file(self, file_id, max_bytes):
        with self._lock:
            return _fake_file(self.files, file_id, max_bytes)


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
                "bot_id": 900001, "username": "aot_test_bot", "updates": [], "sent": [], "reactions": [], "outcomes": [], "poll_offsets": []}
            data.setdefault("reactions", [])
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

    def send_photo(self, chat_id, data, *, caption=None, filename=None, mime=None,
                   thread_id=None, reply_to=None):
        return self._send_media("photo", chat_id, data, caption, filename, mime, thread_id, reply_to)

    def send_document(self, chat_id, data, *, caption=None, filename=None, mime=None,
                      thread_id=None, reply_to=None):
        return self._send_media("document", chat_id, data, caption, filename, mime, thread_id, reply_to)

    def _send_media(self, kind, chat_id, data, caption, filename, mime, thread_id, reply_to):
        payload = bytes(data)
        with self._data() as stored:
            outcome = stored["outcomes"].pop(0) if stored["outcomes"] else "sent"
            result = {"message_id": len(stored["sent"])+1, "chat": {"id": chat_id}, "caption": caption,
                      "message_thread_id": thread_id, "reply_to_message_id": reply_to,
                      "media": {"kind": kind, "sha256": hashlib.sha256(payload).hexdigest(),
                                "size": len(payload), "filename": filename, "mime": mime}}
            if outcome not in ("reject", "rate_limit"):
                stored["sent"].append(result)
        if outcome == "reject":
            raise TransportError(403)
        if outcome == "rate_limit":
            raise TransportError(429, 30)
        if outcome == "unknown":
            raise UnknownSend()
        if outcome == "crash":
            raise SystemExit("fake crash after acceptance")
        return result

    def set_message_reaction(self, chat_id, message_id, emoji):
        with self._data() as data:
            outcome = data["outcomes"].pop(0) if data["outcomes"] else "sent"
            if outcome not in ("reject", "rate_limit"):
                data["reactions"].append({"chat_id": chat_id, "message_id": message_id, "emoji": emoji})
        if outcome == "reject":
            raise TransportError(403)
        if outcome == "rate_limit":
            raise TransportError(429, 30)
        if outcome == "unknown":
            raise UnknownSend()
        if outcome == "crash":
            raise SystemExit("fake crash after acceptance")
        return True

    def get_chat_member(self, chat_id, user_id):
        with self._data() as data:
            return _fake_chat_member(data.get("chat_members", {}), chat_id, user_id)

    def download_file(self, file_id, max_bytes):
        with self._data() as data:
            return _fake_file(data.get("files", {}), file_id, max_bytes)


def make_transport(state_dir, *, fake=False):
    return FileFakeTransport(Path(state_dir) / "fake-transport.json") if fake else TelegramTransport(load_token(state_dir))
