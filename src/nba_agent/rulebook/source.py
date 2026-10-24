"""Fetch the official rulebook PDF and pull its text page by page."""

from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path

import requests
from pypdf import PdfReader

# Linked from https://official.nba.com/rulebook ("2026-27 NBA Rulebook").
RULEBOOK_SEASON = "2026-27"
RULEBOOK_URL = (
    "https://ak-static.cms.nba.com/wp-content/uploads/sites/4/2026/10/"
    "2026-27-Official-NBA-Playing-Rules.pdf"
)
DEFAULT_PDF_PATH = Path("data/raw/rulebook") / f"{RULEBOOK_SEASON}-Official-NBA-Playing-Rules.pdf"

_PAGE_HEADER = re.compile(r"^\s*-\s*(\d+)\s*-\s*$")


def download(path: Path = DEFAULT_PDF_PATH, url: str = RULEBOOK_URL, force: bool = False) -> Path:
    """Download the PDF once; the file is gitignored and never republished."""
    if path.exists() and not force:
        return path
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    if not response.content.startswith(b"%PDF"):
        raise ValueError(f"{url} did not return a PDF")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(response.content)
    return path


def read_pages(path: Path) -> list[tuple[int, str]]:
    """Return (printed page number, text) for each page, header line removed.

    The PDF prints '- N -' at the top of each page; that number is what the
    rulebook's table of contents uses, so it's the one we cite.
    """
    # pypdf warns about decorative cover fonts; the body text is unaffected.
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    pages = []
    for index, page in enumerate(PdfReader(path).pages, start=1):
        # NFKC turns typographic ligatures ('ﬀ' in 'Playoﬀs') back into letters.
        lines = unicodedata.normalize("NFKC", page.extract_text() or "").splitlines()
        number = index
        if lines and (match := _PAGE_HEADER.match(lines[0])):
            number = int(match.group(1))
            lines = lines[1:]
        pages.append((number, "\n".join(lines)))
    return pages
