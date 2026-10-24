"""Hybrid rulebook search: embeddings + BM25, merged with reciprocal rank fusion.

Embeddings catch paraphrases ("take someone down on a fast break" -> Transition
Take Foul); BM25 catches the rulebook's exact wording ("hang on the rim").
The index is ~250 chunks, so both rankers run in memory: no connection is held
open between searches, and no extra dependency is needed.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

import duckdb
import numpy as np

from nba_agent.rulebook.embed import Embedder

RRF_K = 60  # standard reciprocal-rank-fusion constant
CANDIDATES = 30  # how deep each ranker looks before fusion
_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "a an and are as at be by can do does for from has have how if in is it its of on or "
    "shall that the their there this to was what when where which who will with".split()
)


@dataclass(frozen=True)
class RuleHit:
    chunk_id: str
    rule: str
    section: str
    page_start: int
    page_end: int
    text: str
    score: float

    @property
    def citation(self) -> str:
        pages = f"p. {self.page_start}" if self.page_start == self.page_end else f"pp. {self.page_start}-{self.page_end}"
        return f"{self.rule}, {self.section}, {pages}" if self.section else f"{self.rule}, {pages}"


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS]


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.docs = [Counter(tokenize(d)) for d in docs]
        self.lengths = [sum(d.values()) for d in self.docs]
        self.avg_len = sum(self.lengths) / max(len(self.docs), 1)
        df = Counter(term for doc in self.docs for term in doc)
        n = len(self.docs)
        self.idf = {term: math.log(1 + (n - f + 0.5) / (f + 0.5)) for term, f in df.items()}

    def scores(self, query: str) -> list[float]:
        terms = tokenize(query)
        out = []
        for doc, length in zip(self.docs, self.lengths):
            s = 0.0
            for t in terms:
                tf = doc.get(t, 0)
                if tf:
                    s += self.idf[t] * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * length / self.avg_len))
            out.append(s)
        return out


def index_version(con: duckdb.DuckDBPyConnection) -> tuple[str, int, str] | None:
    """(model, dim, built_at) of the current index, or None if it hasn't been built."""
    try:
        row = con.execute("select model, dim, cast(built_at as varchar) from rulebook.meta").fetchone()
    except duckdb.CatalogException:
        return None
    return tuple(row) if row else None


class RulebookSearcher:
    """In-memory copy of the rulebook index; each search embeds the query and fuses two rankings."""

    def __init__(self, rows: list[tuple], vectors: np.ndarray, embedder: Embedder, version: tuple) -> None:
        self.embedder = embedder
        self.version = version
        self.chunks = [r[:6] for r in rows]  # chunk_id, rule, section, page_start, page_end, text
        self.vectors = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
        self.bm25 = BM25([f"{r[6]}\n{r[5]}" for r in rows])

    @classmethod
    def load(cls, con: duckdb.DuckDBPyConnection, embedder: Embedder) -> RulebookSearcher:
        version = index_version(con)
        if version is None:
            raise RuntimeError("rulebook index not built yet; run nba-rulebook")
        model, dim, _ = version
        if (model, dim) != (embedder.name, embedder.dim):
            raise RuntimeError(
                f"index was built with {model} ({dim}d) but searching with "
                f"{embedder.name} ({embedder.dim}d); rebuild with nba-rulebook"
            )
        rows = con.execute(
            "select chunk_id, rule, section, page_start, page_end, text, heading, embedding "
            "from rulebook.chunks order by chunk_id"
        ).fetchall()
        vectors = np.array([r[7] for r in rows], dtype=np.float32)
        return cls([r[:7] for r in rows], vectors, embedder, version)

    def _vector_ranking(self, query: str) -> list[int]:
        q = np.asarray(self.embedder.embed_query(query), dtype=np.float32)
        sims = self.vectors @ (q / np.linalg.norm(q))
        return list(np.argsort(-sims, kind="stable")[:CANDIDATES])

    def _keyword_ranking(self, query: str) -> list[int]:
        scores = self.bm25.scores(query)
        order = sorted((i for i, s in enumerate(scores) if s > 0), key=lambda i: (-scores[i], i))
        return order[:CANDIDATES]

    def search(self, query: str, k: int = 5) -> list[RuleHit]:
        fused: dict[int, float] = {}
        for ranking in (self._vector_ranking(query), self._keyword_ranking(query)):
            for rank, i in enumerate(ranking, start=1):
                fused[int(i)] = fused.get(int(i), 0.0) + 1.0 / (RRF_K + rank)
        best = sorted(fused.items(), key=lambda x: (-x[1], x[0]))[:k]
        return [RuleHit(*self.chunks[i], score=round(score, 4)) for i, score in best]
