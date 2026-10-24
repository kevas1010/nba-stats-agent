"""Store rulebook chunks and their embeddings in DuckDB (schema `rulebook`)."""

from __future__ import annotations

from datetime import datetime, timezone

import duckdb
import pandas as pd

from nba_agent.rulebook.chunk import Chunk
from nba_agent.rulebook.embed import Embedder

SCHEMA = "rulebook"


def build_index(
    con: duckdb.DuckDBPyConnection,
    chunks: list[Chunk],
    embedder: Embedder,
    *,
    season: str,
    source_url: str,
) -> int:
    """Replace rulebook.chunks and rulebook.meta with a fresh index."""
    if not chunks:
        raise ValueError("no chunks to index")
    vectors = embedder.embed_documents([c.embedding_text() for c in chunks])
    if any(len(v) != embedder.dim for v in vectors):
        raise ValueError(f"embedder returned vectors that aren't {embedder.dim}-dimensional")

    frame = pd.DataFrame({
        "chunk_id": [c.chunk_id for c in chunks],
        "rule": [c.rule for c in chunks],
        "section": [c.section for c in chunks],
        "heading": [c.heading for c in chunks],
        "page_start": [c.page_start for c in chunks],
        "page_end": [c.page_end for c in chunks],
        "text": [c.text for c in chunks],
        "embedding": vectors,
    })
    meta = pd.DataFrame([{
        "model": embedder.name,
        "dim": embedder.dim,
        "season": season,
        "source_url": source_url,
        "chunk_count": len(chunks),
        "built_at": datetime.now(timezone.utc),
    }])

    con.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    con.register("incoming_chunks", frame)
    con.register("incoming_meta", meta)
    try:
        con.execute("BEGIN")
        # A fixed-size array column lets DuckDB compute array_cosine_similarity.
        con.execute(f"""
            CREATE OR REPLACE TABLE {SCHEMA}.chunks AS
            SELECT * REPLACE (CAST(embedding AS FLOAT[{embedder.dim}]) AS embedding)
            FROM incoming_chunks
        """)
        con.execute(f"CREATE OR REPLACE TABLE {SCHEMA}.meta AS SELECT * FROM incoming_meta")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.unregister("incoming_chunks")
        con.unregister("incoming_meta")
    return len(chunks)
