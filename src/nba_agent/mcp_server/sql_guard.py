"""Allowlist check for agent-written SQL.

The agent may only run a single read-only query against the mart tables.
This is the first of two layers: the warehouse connection itself is also
opened read-only with external file/network access disabled (see warehouse.py).
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

# schema -> tables the agent may read.
ALLOWED_TABLES: dict[str, frozenset[str]] = {
    "marts": frozenset({"player_game_stats", "team_pace"}),
    "reference": frozenset({"all_star_games"}),
}

# Scalar functions that reach outside the query (env, settings, files).
BLOCKED_FUNCTIONS = frozenset({
    "getenv", "current_setting", "glob", "sniff_csv", "query", "query_table",
    "load_extension", "duckdb_secrets", "duckdb_settings", "duckdb_extensions",
})
BLOCKED_FUNCTION_PREFIXES = ("read_", "parquet_", "duckdb_")

# Statement types that must never appear anywhere in the tree.
_FORBIDDEN_NODES = tuple(
    getattr(exp, name)
    for name in (
        "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter",
        "Copy", "Attach", "Detach", "Pragma", "Set", "Command", "Install",
        "Use", "Transaction", "Commit", "Rollback", "Into",
    )
    if hasattr(exp, name)
)


class UnsafeQueryError(ValueError):
    """Raised when SQL fails the read-only allowlist."""


def _allowed_unqualified() -> set[str]:
    return {table for tables in ALLOWED_TABLES.values() for table in tables}


def check_query(sql: str) -> str:
    """Return the query unchanged if it is safe to run, else raise UnsafeQueryError."""
    if not sql or not sql.strip():
        raise UnsafeQueryError("empty query")

    try:
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except ParseError as exc:
        raise UnsafeQueryError(f"could not parse SQL: {exc}") from exc

    if len(statements) != 1:
        raise UnsafeQueryError("exactly one statement is allowed")
    tree = statements[0]

    if not isinstance(tree, exp.Query):
        raise UnsafeQueryError(f"only SELECT queries are allowed, got {tree.key.upper()}")

    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise UnsafeQueryError(f"{node.key.upper()} is not allowed")

    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    unqualified_ok = _allowed_unqualified() | cte_names

    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise UnsafeQueryError("table functions are not allowed in FROM")
        name, schema, catalog = table.name.lower(), table.db.lower(), table.catalog.lower()
        if catalog:
            raise UnsafeQueryError(f"catalog-qualified tables are not allowed: {table.sql()}")
        if schema:
            if name not in ALLOWED_TABLES.get(schema, ()):
                raise UnsafeQueryError(f"table not allowed: {schema}.{name}")
        elif name not in unqualified_ok:
            raise UnsafeQueryError(f"table not allowed: {name}")

    for func in tree.find_all(exp.Func):
        fname = (func.name if isinstance(func, exp.Anonymous) else func.sql_name()).lower()
        if fname in BLOCKED_FUNCTIONS or fname.startswith(BLOCKED_FUNCTION_PREFIXES):
            raise UnsafeQueryError(f"function not allowed: {fname}")

    return sql
