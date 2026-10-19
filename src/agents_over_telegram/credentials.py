"""Protected-file fallback. Secrets never enter MCP arguments or diagnostics."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import tempfile


class CredentialError(Exception):
    def __init__(self, code="unsafe_credentials"):
        self.code = code
        super().__init__(code)


def private_directory(path):
    path = Path(path).absolute()
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise CredentialError("unsafe_state_directory")
    except OSError:
        raise CredentialError("state_directory_unavailable") from None
    return path


def read_private_json(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise CredentialError()
            raw = stream.read(65537)
            if len(raw) > 65536:
                raise CredentialError("invalid_credentials")
            value = json.loads(raw)
        if not isinstance(value, dict):
            raise CredentialError("invalid_credentials")
        return value
    except (OSError, ValueError, UnicodeError):
        raise CredentialError("credentials_unavailable") from None


def write_private_json(path, value, *, overwrite=False):
    path = Path(path).absolute()
    private_directory(path.parent)
    if path.exists() or path.is_symlink():
        if not overwrite:
            raise CredentialError("credentials_already_exist")
        read_private_json(path)
    temp = None
    try:
        fd, temp = tempfile.mkstemp(prefix=".aot-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temp, path)
        else:
            # link is an atomic no-replace publication; no race overwrites a secret.
            os.link(temp, path)
            os.unlink(temp)
        temp = None
        dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        raise CredentialError("credential_write_failed") from None
    finally:
        if temp is not None:
            os.unlink(temp)


def validate_token(token):
    if not isinstance(token, str) or not re.fullmatch(r"[0-9]{1,20}:[A-Za-z0-9_-]{20,200}", token):
        raise CredentialError("invalid_token_format")
    return token


def load_token(state_dir):
    private_directory(state_dir)
    return validate_token(read_private_json(Path(state_dir) / "credentials.json").get("token"))
