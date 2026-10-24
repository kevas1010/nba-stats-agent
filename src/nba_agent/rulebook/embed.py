"""Embedding models for rulebook chunks and queries."""

from __future__ import annotations

import os
import warnings
from pathlib import Path
from typing import Protocol

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_CACHE_DIR = Path("data/models")


class Embedder(Protocol):
    name: str
    dim: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class FastEmbedder:
    """Local ONNX embeddings via fastembed: no API key, no per-query cost.

    bge models expect a retrieval prefix on queries, which query_embed adds.
    Vectors come back unit-length, so cosine similarity is a dot product.
    """

    def __init__(self, name: str = DEFAULT_MODEL, cache_dir: Path | None = None) -> None:
        from fastembed import TextEmbedding

        cache = cache_dir or Path(os.getenv("NBA_MODEL_CACHE", DEFAULT_CACHE_DIR))
        with warnings.catch_warnings():
            # huggingface_hub warns that Windows can't symlink its cache; harmless.
            warnings.simplefilter("ignore", UserWarning)
            self._model = TextEmbedding(name, cache_dir=str(cache))
        self.name = name
        self.dim = self._model.embedding_size

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self._model.passage_embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        return next(iter(self._model.query_embed(text))).tolist()
