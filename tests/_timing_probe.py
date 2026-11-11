"""Temporary CI probe: where does the test suite spend time on each OS? Not for merging."""
import os
import sqlite3
import subprocess
import threading
import time

from agents_over_telegram import compat

_totals = {}
_lock = threading.Lock()


def _add(name, seconds):
    with _lock:
        count, total = _totals.get(name, (0, 0.0))
        _totals[name] = (count + 1, total + seconds)


def _timed(name, fn):
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            _add(name, time.perf_counter() - start)
    return wrapper


compat.restrict_directory = _timed('icacls (restrict_directory)', compat.restrict_directory)
compat._user_sid = _timed('whoami (_user_sid)', compat._user_sid)
os.fsync = _timed('os.fsync', os.fsync)
subprocess.run = _timed('subprocess.run', subprocess.run)
_popen_init = subprocess.Popen.__init__
subprocess.Popen.__init__ = _timed('Popen start', _popen_init)


class _TimedConnection(sqlite3.Connection):
    def execute(self, sql, *args):
        start = time.perf_counter()
        try:
            return super().execute(sql, *args)
        finally:
            word = sql.strip().split(None, 1)[0].upper() if sql.strip() else ''
            if word in ('COMMIT', 'BEGIN', 'PRAGMA', 'CREATE'):
                _add(f'sqlite {word}', time.perf_counter() - start)


_connect = sqlite3.connect


def _timed_connect(*args, **kwargs):
    kwargs.setdefault('factory', _TimedConnection)
    start = time.perf_counter()
    try:
        return _connect(*args, **kwargs)
    finally:
        _add('sqlite connect', time.perf_counter() - start)


sqlite3.connect = _timed_connect


def pytest_terminal_summary(terminalreporter):
    terminalreporter.write_line('==== timing probe (in-process) ====')
    for name, (count, total) in sorted(_totals.items(), key=lambda item: -item[1][1]):
        terminalreporter.write_line(f'{total:8.1f}s  {count:7d}x  {1000 * total / max(count, 1):7.1f}ms avg  {name}')
