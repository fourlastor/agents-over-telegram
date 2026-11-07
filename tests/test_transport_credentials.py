import io
import json
import hashlib
import os
import urllib.error

import pytest

from agents_over_telegram.credentials import CredentialError, load_token, private_directory, read_private_json, write_private_json
from agents_over_telegram.transport import FakeTransport, FileFakeTransport, TelegramTransport, TransportError, UnknownSend

SECRET = "987654:FAKE_SECRET_MUST_NEVER_ESCAPE_123456789"


class Response(io.BytesIO):
    pass


class Opener:
    def __init__(self, value):
        self.value = value
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        if isinstance(self.value, BaseException):
            raise self.value
        return Response(self.value)


def test_private_file_modes_and_roundtrip(tmp_path):
    root = private_directory(tmp_path / "state")
    write_private_json(root / "credentials.json", {"token": SECRET})
    assert os.stat(root).st_mode & 0o777 == 0o700
    assert os.stat(root / "credentials.json").st_mode & 0o777 == 0o600
    assert load_token(root) == SECRET
    with pytest.raises(CredentialError, match="already_exist"):
        write_private_json(root / "credentials.json", {"token": "different"})
    os.chmod(root / "credentials.json", 0o644)
    with pytest.raises(CredentialError):
        load_token(root)


def test_symlink_and_hardlink_rejected(tmp_path):
    root = private_directory(tmp_path / "state")
    write_private_json(root / "original", {"token": SECRET})
    (root / "link").symlink_to(root / "original")
    with pytest.raises(CredentialError):
        read_private_json(root / "link")
    os.link(root / "original", root / "hardlink")
    with pytest.raises(CredentialError):
        read_private_json(root / "hardlink")
    with pytest.raises(CredentialError):
        write_private_json(root / "link", {"x": 1}, overwrite=True)
    (tmp_path / "dirlink").symlink_to(root)
    with pytest.raises(CredentialError):
        private_directory(tmp_path / "dirlink")


@pytest.mark.parametrize("error", [TimeoutError(SECRET), OSError(SECRET), ValueError(SECRET)])
def test_errors_do_not_leak_secret(error):
    transport = TelegramTransport(SECRET, opener=Opener(error))
    with pytest.raises(UnknownSend) as exc:
        transport.send_message(-100, "test")
    assert SECRET not in str(exc.value)
    assert exc.value.__cause__ is None
    with pytest.raises(TransportError) as exc:
        transport.get_me()
    assert SECRET not in str(exc.value)


@pytest.mark.parametrize("status,exception", [(400, TransportError), (403, TransportError), (429, TransportError), (500, UnknownSend), (302, UnknownSend)])
def test_http_outcomes(status, exception):
    error = urllib.error.HTTPError("https://api.telegram.org/bot"+SECRET, status, SECRET, {},
        io.BytesIO(json.dumps({"description": SECRET, "parameters": {"retry_after": 17}}).encode()))
    transport = TelegramTransport(SECRET, opener=Opener(error))
    with pytest.raises(exception) as exc:
        transport.send_message(-100, "test")
    assert SECRET not in str(exc.value)
    if status == 429:
        assert exc.value.retry_after == 17


MIGRATED = {"ok": False, "error_code": 400, "description": SECRET,
            "parameters": {"migrate_to_chat_id": -1001234567890}}


@pytest.mark.parametrize("as_http_error", [True, False])
def test_rejection_names_the_supergroup_a_group_became(as_http_error):
    body = json.dumps(MIGRATED).encode()
    answer = (urllib.error.HTTPError("https://api.telegram.org/bot"+SECRET, 400, SECRET, {}, io.BytesIO(body))
              if as_http_error else body)
    with pytest.raises(TransportError) as exc:
        TelegramTransport(SECRET, opener=Opener(answer)).send_message(-100, "test")
    assert (exc.value.code, exc.value.migrate_to_chat_id) == (400, -1001234567890)
    assert SECRET not in str(exc.value)


@pytest.mark.parametrize("parameters", [{}, {"migrate_to_chat_id": "-100123"}, {"migrate_to_chat_id": 5},
                                        {"migrate_to_chat_id": True}, "not a mapping"])
def test_rejection_without_a_valid_migration_target_has_none(parameters):
    body = json.dumps({"ok": False, "error_code": 400, "parameters": parameters}).encode()
    error = urllib.error.HTTPError("https://api.telegram.org/bot"+SECRET, 400, SECRET, {}, io.BytesIO(body))
    for answer in (body, error):
        with pytest.raises(TransportError) as exc:
            TelegramTransport(SECRET, opener=Opener(answer)).send_message(-100, "test")
        assert exc.value.migrate_to_chat_id is None


def test_only_a_400_carries_a_migration_target():
    body = json.dumps({**MIGRATED, "error_code": 403}).encode()
    with pytest.raises(TransportError) as exc:
        TelegramTransport(SECRET, opener=Opener(body)).send_message(-100, "test")
    assert (exc.value.code, exc.value.migrate_to_chat_id) == (403, None)


@pytest.mark.parametrize("body", [b"not json", b"{}", b'{"ok":true,"result":{}}', b'{"ok":false,"error_code":500}', b"[]"])
def test_malformed_send_response_is_unknown(body):
    with pytest.raises(UnknownSend):
        TelegramTransport(SECRET, opener=Opener(body)).send_message(-100, "test")


def test_send_shape_plain_text_no_paid_features():
    opener = Opener(b'{"ok":true,"result":{"message_id":7}}')
    result = TelegramTransport(SECRET, opener=opener).send_message(-100, "[Alpha] hello", 42, 9)
    assert result == {"message_id": 7}
    assert json.loads(opener.requests[0].data) == {"chat_id": -100, "text": "[Alpha] hello", "message_thread_id": 42,
        "reply_parameters": {"message_id": 9}, "link_preview_options": {"is_disabled": True}}


@pytest.mark.parametrize("method,field,filename,mime", [
    ("send_photo", "photo", "shot.png", "image/png"),
    ("send_document", "document", "shot.gif", "image/gif"),
])
def test_send_media_uploads_multipart(method, field, filename, mime):
    opener = Opener(b'{"ok":true,"result":{"message_id":9}}')
    transport = TelegramTransport(SECRET, opener=opener)
    data = b"\x89PNG\r\n\x1a\nbody-bytes"
    result = getattr(transport, method)(-100, data, caption="cap", filename=filename,
                                        mime=mime, thread_id=4, reply_to=2)
    assert result == {"message_id": 9}
    request = opener.requests[0]
    assert request.headers["Content-type"].startswith("multipart/form-data; boundary=----aot")
    body = request.data
    assert b'name="chat_id"\r\n\r\n-100\r\n' in body
    assert b'name="caption"\r\n\r\ncap\r\n' in body
    assert b'name="message_thread_id"\r\n\r\n4\r\n' in body
    assert b'name="reply_parameters"\r\n\r\n{"message_id": 2}\r\n' in body
    assert f'Content-Disposition: form-data; name="{field}"; filename="{filename}"'.encode() in body
    assert f"Content-Type: {mime}\r\n\r\n".encode() in body
    assert data in body
    assert SECRET not in body.decode("latin-1")


def test_send_media_malformed_response_is_unknown():
    with pytest.raises(UnknownSend):
        TelegramTransport(SECRET, opener=Opener(b'{"ok":true,"result":{}}')).send_photo(-100, b"\x89PNG\r\n\x1a\n")


def test_fakes_record_media_and_outcomes():
    fake = FakeTransport(outcomes=["unknown"])
    with pytest.raises(UnknownSend):
        fake.send_photo(-100, b"\x89PNG\r\n\x1a\n", caption="cap", filename="shot.png",
                        mime="image/png", thread_id=3, reply_to=5)
    assert len(fake.sent) == 1
    media = fake.sent[0]["media"]
    assert media["kind"] == "photo" and media["data"] == b"\x89PNG\r\n\x1a\n"
    assert hashlib.sha256(b"\x89PNG\r\n\x1a\n").hexdigest() == media["sha256"]
    fake.send_document(-100, b"GIF89a", filename="loop.gif", mime="image/gif")
    assert fake.sent[1]["media"]["kind"] == "document"


def test_fake_no_echo_and_unknown_acceptance():
    fake = FakeTransport(outcomes=["unknown"])
    with pytest.raises(UnknownSend):
        fake.send_message(-100, "hello")
    assert len(fake.sent) == 1
    assert fake.get_updates(0) == []


def test_persistent_fake_uncertainty(tmp_path):
    path = private_directory(tmp_path/"state")/"fake-transport.json"
    fake = FileFakeTransport(path)
    fake.get_me()
    data = read_private_json(path)
    data["outcomes"] = ["unknown"]
    write_private_json(path, data, overwrite=True)
    with pytest.raises(UnknownSend):
        fake.send_message(-100, "hello")
    assert len(read_private_json(path)["sent"]) == 1
    assert FileFakeTransport(path).get_updates(0) == []
