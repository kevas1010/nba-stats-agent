import duckdb
import pytest

from nba_agent.rulebook.chunk import Chunk
from nba_agent.rulebook.index import build_index


class FakeEmbedder:
    name = "fake"
    dim = 3

    def embed_documents(self, texts):
        return [[1.0, float(len(t)), 0.0] for t in texts]

    def embed_query(self, text):
        return [1.0, 0.0, 0.0]


CHUNKS = [
    Chunk("c0000", "Rule 7 — Shot Clock", "Section I — Definition", 28, 28, "a. 24 seconds."),
    Chunk("c0001", "Rule 10 — Violations", "Section XIII — Traveling", 37, 38, "a. Moving the pivot foot."),
]


def test_build_index_writes_chunks_and_meta():
    con = duckdb.connect()
    assert build_index(con, CHUNKS, FakeEmbedder(), season="2026-27", source_url="https://example") == 2

    rows = con.execute("select chunk_id, heading, page_start, page_end from rulebook.chunks order by 1").fetchall()
    assert rows == [
        ("c0000", "Rule 7 — Shot Clock > Section I — Definition", 28, 28),
        ("c0001", "Rule 10 — Violations > Section XIII — Traveling", 37, 38),
    ]
    assert con.execute("select typeof(embedding) from rulebook.chunks limit 1").fetchone()[0] == "FLOAT[3]"
    assert con.execute("select model, dim, season, chunk_count from rulebook.meta").fetchone() == ("fake", 3, "2026-27", 2)


def test_embeddings_include_the_heading():
    con = duckdb.connect()
    build_index(con, CHUNKS[:1], FakeEmbedder(), season="2026-27", source_url="x")
    # FakeEmbedder encodes text length; heading + newline + body is longer than the body alone.
    length = con.execute("select embedding[2] from rulebook.chunks").fetchone()[0]
    assert length == len(CHUNKS[0].embedding_text())


def test_rebuild_replaces_previous_index():
    con = duckdb.connect()
    build_index(con, CHUNKS, FakeEmbedder(), season="2026-27", source_url="x")
    build_index(con, CHUNKS[:1], FakeEmbedder(), season="2026-27", source_url="x")
    assert con.execute("select count(*) from rulebook.chunks").fetchone()[0] == 1
    assert con.execute("select count(*) from rulebook.meta").fetchone()[0] == 1


def test_rejects_wrong_dimension_vectors():
    class Broken(FakeEmbedder):
        def embed_documents(self, texts):
            return [[1.0, 2.0] for _ in texts]

    with pytest.raises(ValueError, match="3-dimensional"):
        build_index(duckdb.connect(), CHUNKS, Broken(), season="2026-27", source_url="x")


def test_rejects_empty_chunk_list():
    with pytest.raises(ValueError, match="no chunks"):
        build_index(duckdb.connect(), [], FakeEmbedder(), season="2026-27", source_url="x")
