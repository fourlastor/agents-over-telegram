"""Offline checks for bounded lock recovery and transaction replay safety."""
import contextlib
import sqlite3

import pytest

from agents_over_telegram.core import Core, CoreError
from agents_over_telegram.mcp_server import _exception_code


def storage_error(code):
    error = sqlite3.OperationalError('private fixture details must not escape')
    error.sqlite_errorcode = code
    return error


def transaction_core(failures):
    commands = []

    class Connection:
        def execute(self, statement):
            commands.append(statement)
            error = failures.get((statement, commands.count(statement)))
            if error is not None:
                raise error

    core = Core.__new__(Core)

    @contextlib.contextmanager
    def database():
        yield Connection()

    core._db = database
    return core, commands


def test_busy_begin_retries_once_before_running_work():
    core, commands = transaction_core({('BEGIN IMMEDIATE', 1): storage_error(sqlite3.SQLITE_BUSY)})
    work_count = 0
    with core._tx():
        work_count += 1
    assert work_count == 1
    assert commands == ['BEGIN IMMEDIATE', 'BEGIN IMMEDIATE', 'COMMIT']


def test_persistent_busy_does_not_start_or_replay_work():
    core, commands = transaction_core({
        ('BEGIN IMMEDIATE', 1): storage_error(sqlite3.SQLITE_BUSY),
        ('BEGIN IMMEDIATE', 2): storage_error(sqlite3.SQLITE_BUSY),
    })
    with pytest.raises(sqlite3.OperationalError):
        with core._tx():
            pytest.fail('Work must not start before transaction acquisition')
    assert commands == ['BEGIN IMMEDIATE', 'BEGIN IMMEDIATE']


def test_nonbusy_begin_does_not_retry():
    core, commands = transaction_core({('BEGIN IMMEDIATE', 1): storage_error(sqlite3.SQLITE_READONLY)})
    with pytest.raises(sqlite3.OperationalError):
        with core._tx():
            pytest.fail('Work must not start after failed acquisition')
    assert commands == ['BEGIN IMMEDIATE']


def test_busy_body_rolls_back_without_replay():
    core, commands = transaction_core({})
    work_count = 0
    with pytest.raises(sqlite3.OperationalError):
        with core._tx():
            work_count += 1
            raise storage_error(sqlite3.SQLITE_BUSY)
    assert work_count == 1
    assert commands == ['BEGIN IMMEDIATE', 'ROLLBACK']


def test_busy_commit_rolls_back_without_replay():
    core, commands = transaction_core({('COMMIT', 1): storage_error(sqlite3.SQLITE_BUSY)})
    work_count = 0
    with pytest.raises(sqlite3.OperationalError):
        with core._tx():
            work_count += 1
    assert work_count == 1
    assert commands == ['BEGIN IMMEDIATE', 'COMMIT', 'ROLLBACK']


@pytest.mark.parametrize(('code', 'expected'), [
    (sqlite3.SQLITE_BUSY, 'storage_busy'),
    (sqlite3.SQLITE_LOCKED, 'storage_busy'),
    (sqlite3.SQLITE_BUSY | (2 << 8), 'storage_busy'),
    (sqlite3.SQLITE_READONLY, 'storage_error'),
])
def test_storage_error_classification_preserves_fixed_messages(tmp_path, code, expected):
    core = Core.__new__(Core)
    core.database = tmp_path / 'synthetic.sqlite3'
    with pytest.raises(CoreError) as caught:
        with core._db():
            raise storage_error(code)
    assert caught.value.code == expected
    assert str(caught.value) == expected


def test_storage_busy_is_an_admitted_mcp_error():
    assert _exception_code(CoreError('storage_busy')) == 'storage_busy'
