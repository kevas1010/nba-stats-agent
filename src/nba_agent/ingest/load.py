"""Land fetched DataFrames as Parquet files and in the DuckDB raw schema."""

from __future__ import annotations

import re
from pathlib import Path

import duckdb
import pandas as pd

RAW_SCHEMA = "raw"
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def write_parquet(df: pd.DataFrame, raw_dir: Path, table: str, season: str, season_type: str | None = None) -> Path:
    """data/raw/<table>/<season>[_<type>].parquet; overwrites on re-run."""
    suffix = f"_{season_type.lower().replace(' ', '_')}" if season_type else ""
    path = raw_dir / table / f"{season}{suffix}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    return path


def load_frame(con: duckdb.DuckDBPyConnection, df: pd.DataFrame, table: str, partition: dict[str, str]) -> int:
    """Replace one partition (e.g. a season + season type) of raw.<table> with df.

    Re-running ingestion for the same season is idempotent: old rows for that
    partition are deleted first, other seasons are left alone.
    """
    if not _IDENT.match(table) or not all(_IDENT.match(c) for c in partition):
        raise ValueError(f"unsafe identifier in {table!r} / {list(partition)!r}")

    con.execute(f"CREATE SCHEMA IF NOT EXISTS {RAW_SCHEMA}")
    target = f"{RAW_SCHEMA}.{table}"
    con.register("incoming", df)
    try:
        con.execute(f"CREATE TABLE IF NOT EXISTS {target} AS SELECT * FROM incoming LIMIT 0")
        where = " AND ".join(f"{col} = ?" for col in partition)
        con.execute(f"DELETE FROM {target} WHERE {where}", list(partition.values()))
        con.execute(f"INSERT INTO {target} BY NAME SELECT * FROM incoming")
    finally:
        con.unregister("incoming")
    return len(df)
