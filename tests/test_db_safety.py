"""Tests for the throwaway-database guard (memory._db_safety).

The guard refuses to operate on any database whose name does not end in ``_test``
or ``_demo``. This proves the test suite can never accidentally TRUNCATE the real
application database.
"""
from memory._db_safety import database_name_from_url, guard_db_name


def test_guard_accepts_test_and_demo_databases():
    for name in ("memory_test", "x_test", "y_demo", "memory_demo"):
        assert guard_db_name(name), f"{name!r} should be allowed"


def test_guard_refuses_real_database():
    for name in ("memory", "production", "public"):
        assert not guard_db_name(name), f"{name!r} should be refused"


def test_database_name_from_url_parses_target():
    url = "postgresql+asyncpg://memory:memory@localhost:5433/memory_test"
    assert database_name_from_url(url) == "memory_test"
