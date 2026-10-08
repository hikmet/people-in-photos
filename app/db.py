"""The SQLite database: one file on the data volume, shared by the app and the worker.

Every transaction starts with BEGIN IMMEDIATE, taking SQLite's single write lock up front. A
transaction that read first and wrote later could otherwise fail outright when the other process
wrote in between; this way it waits its turn (`busy_timeout`) instead. WAL keeps readers out of
each other's way, and the lock is short as long as slow work (image processing, the upload's QR
scans) happens outside a transaction, which is how the worker and the upload route are written.
"""

import sqlite3
from contextlib import closing
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.config import settings

SCHEMA = Path(__file__).with_name("schema.sql")
BUSY_TIMEOUT_MS = 60_000


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "connect")
    def configure(connection, _):
        # SQLAlchemy issues the BEGIN itself (below), so the driver must not open transactions.
        connection.isolation_level = None
        for pragma in ("journal_mode = WAL", "foreign_keys = ON", f"busy_timeout = {BUSY_TIMEOUT_MS}"):
            connection.execute("PRAGMA " + pragma)

    @event.listens_for(engine, "begin")
    def begin(connection):
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


engine = connect(settings().database_path)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def create_schema():
    """Create every table, trigger and index that does not exist yet. Safe to run from the app and
    the worker at once: the script is one transaction and every statement is IF NOT EXISTS."""
    with closing(sqlite3.connect(settings().database_path, timeout=BUSY_TIMEOUT_MS / 1000)) as connection:
        connection.executescript(SCHEMA.read_text())


def get_db():
    with SessionLocal() as session:
        yield session
