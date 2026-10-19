#!/usr/bin/env python3
"""Fail closed until an operator selects one scoped local MCP profile."""
from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Mapping


class ProfileError(ValueError):
    """A fixed diagnostic that deliberately excludes environment contents."""


def build_command(env: Mapping[str, str]) -> list[str]:
    selected: dict[str, str] = {}
    for key in ("AOT_EXECUTABLE", "AOT_STATE_DIR", "AOT_CLIENT_FILE"):
        value = env.get(key, "")
        if not value or not Path(value).is_absolute() or "\x00" in value:
            raise ProfileError("Configure absolute AOT_EXECUTABLE, AOT_STATE_DIR and AOT_CLIENT_FILE paths for one scoped client.")
        selected[key] = value
    if not Path(selected["AOT_EXECUTABLE"]).is_file() or not os.access(selected["AOT_EXECUTABLE"], os.X_OK):
        raise ProfileError("The selected Agents over Telegram executable is unavailable.")
    if not Path(selected["AOT_STATE_DIR"]).is_dir() or not Path(selected["AOT_CLIENT_FILE"]).is_file():
        raise ProfileError("Initialize the selected state directory and client capability file before starting MCP.")
    return [selected["AOT_EXECUTABLE"], "--state-dir", selected["AOT_STATE_DIR"], "mcp", "--client-file", selected["AOT_CLIENT_FILE"]]


def main() -> int:
    try:
        command = build_command(os.environ)
        os.execv(command[0], command)
    except ProfileError as exc:
        print(str(exc), file=sys.stderr)
    except OSError:
        print("The selected Agents over Telegram process could not be started.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
