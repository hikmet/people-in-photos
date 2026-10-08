"""app/schema.sql creates the database and app/models.py reads it; nothing generates one from the
other, so this compares the two."""

from sqlalchemy import UniqueConstraint, inspect
from sqlalchemy.dialects import sqlite

from app.db import engine
from app.models import Base


def model_shape(table):
    columns = {c.name: (c.type.compile(sqlite.dialect()), c.nullable) for c in table.columns}
    unique = {(c.name,) for c in table.columns if c.unique} | {
        tuple(c.name for c in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    keys = {(fk.parent.name, fk.column.table.name) for fk in table.foreign_keys}
    indexes = {tuple(c.name for c in index.columns) for index in table.indexes}
    return columns, unique, keys, indexes


def database_shape(inspector, name):
    columns = {
        c["name"]: (c["type"].compile(sqlite.dialect()), c["nullable"]) for c in inspector.get_columns(name)
    }
    unique = {tuple(u["column_names"]) for u in inspector.get_unique_constraints(name)}
    keys = {
        (column, fk["referred_table"])
        for fk in inspector.get_foreign_keys(name)
        for column in fk["constrained_columns"]
    }
    indexes = {tuple(i["column_names"]) for i in inspector.get_indexes(name) if not i.get("unique")}
    return columns, unique, keys, indexes


def test_schema_sql_matches_the_models():
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == set(Base.metadata.tables)
    for name, table in Base.metadata.tables.items():
        assert database_shape(inspector, name) == model_shape(table), name
