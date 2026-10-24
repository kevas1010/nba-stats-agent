"""nba-rulebook: download, chunk and embed the official NBA rulebook into DuckDB.

    nba-rulebook              # download once, then (re)build the index
    nba-rulebook --force      # re-download the PDF first
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import duckdb
from dotenv import load_dotenv

from nba_agent.rulebook.chunk import chunk_pages
from nba_agent.rulebook.embed import FastEmbedder
from nba_agent.rulebook.index import build_index
from nba_agent.rulebook.source import DEFAULT_PDF_PATH, RULEBOOK_SEASON, RULEBOOK_URL, download, read_pages


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=os.getenv("DUCKDB_PATH", "data/nba.duckdb"))
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF_PATH)
    parser.add_argument("--force", action="store_true", help="re-download the PDF")
    args = parser.parse_args(argv)

    pdf = download(args.pdf, force=args.force)
    chunks = chunk_pages(read_pages(pdf))
    print(f"{pdf}: {len(chunks)} chunks from {len({c.heading for c in chunks})} sections")

    started = time.perf_counter()
    embedder = FastEmbedder()
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(args.db) as con:
        n = build_index(con, chunks, embedder, season=RULEBOOK_SEASON, source_url=RULEBOOK_URL)
    print(f"indexed {n} chunks with {embedder.name} in {time.perf_counter() - started:.1f}s -> {args.db}")


if __name__ == "__main__":
    main()
