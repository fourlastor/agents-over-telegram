import io
import json
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
