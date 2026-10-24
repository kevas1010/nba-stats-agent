"""Split rulebook text into section-aware chunks for retrieval.

Each chunk stays inside one rule section and carries its rule, section and
page range, so search results can be cited ("Rule 7, Section II, p. 28").
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

MAX_CHARS = 1200
# Signal-chart and notes pages at the back are captions, not rules.
MIN_PAGE_CHARS = 500

_RULE = re.compile(r"^RULE NO\.\s*(\d+)\s*—\s*(.+)$")
# Body headings use an em-dash with no spaces; the table of contents uses ' – '.
_SECTION = re.compile(r"^Section\s+([IVXL]+)\s*—\s*(.+)$")
# Rule 12 is split into parts ("A.   Technical Foul") that each restart Section numbering.
_PART = re.compile(r"^([A-Z])\.\s+(.{3,40})$")
_COMMENTS = re.compile(r"^COMMENTS ON THE RULES$")
_COMMENT_HEAD = re.compile(r"^([IVX]+|[A-Z])\.\s+([A-Z0-9][A-Z0-9 ,/:’'\-—]+)$")
# Lines that start a new item, so chunk breaks land between items, not mid-sentence.
_ITEM_START = re.compile(r"^(\(?[a-z0-9]{1,3}[.)]|[A-Z]\.\s|EXCEPTION|PENALTY|NOTE)")
_WORD = re.compile(r"[A-Za-z]+")
_SMALL_WORDS = {"a", "an", "and", "as", "at", "by", "for", "in", "is", "of", "on", "or", "the", "to"}


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    rule: str
    section: str
    page_start: int
    page_end: int
    text: str

    @property
    def heading(self) -> str:
        return f"{self.rule} > {self.section}" if self.section else self.rule

    def embedding_text(self) -> str:
        """Heading plus body, so a chunk about 'Section II—Timing' matches 'timing' queries."""
        return f"{self.heading}\n{self.text}"


_ONE_LETTER_WORDS = {"a", "i"}


def _is_fragment(word: str) -> bool:
    return len(word) == 1 and word.lower() not in _ONE_LETTER_WORDS


def build_vocab(pages: list[tuple[int, str]]) -> Counter:
    return Counter(w.lower() for _, text in pages for w in _WORD.findall(text))


def repair_words(line: str, vocab: Counter) -> str:
    """Rejoin words pypdf split at kerned letters ('PENAL TIES', 'T echnical').

    Two letter runs separated by one space are joined when the joined word
    occurs in the rulebook at least as often as the rarer fragment, or when one
    side is a stray letter ('F an'). Real word pairs like 'in to' or 'a way'
    stay apart. Scanning right to left attaches a stray capital to the word
    after it ('no T echnical' -> 'no Technical', not 'noT echnical').
    """
    tokens = re.findall(r"[A-Za-z]+|[^A-Za-z]+", line)
    i = len(tokens) - 3
    while i >= 0:
        left, gap, right = tokens[i], tokens[i + 1], tokens[i + 2]
        after = tokens[i + 3] if i + 3 < len(tokens) else ""
        # 'the re-routing', 'no T -shirts': a hyphenated right half is its own word.
        if left.isalpha() and gap == " " and right.isalpha() and not after.lstrip().startswith("-"):
            joined = (left + right).lower()
            if vocab[joined] and (
                _is_fragment(left)
                or _is_fragment(right)
                or (left.lower() not in _ONE_LETTER_WORDS
                    and vocab[joined] >= min(vocab[left.lower()], vocab[right.lower()]))
            ):
                tokens[i : i + 3] = [left + right]
                i -= 2  # the joined word can now be the right half of a pair
                continue
        i -= 1
    return "".join(tokens)


def title_case(title: str) -> str:
    """'COACH’S CHALLENGE' -> 'Coach’s Challenge'; mixed-case titles are left alone."""
    title = re.sub(r"\s+", " ", title).strip()
    if not title.isupper():
        return title
    parts = re.split(r"([ —/\-])", title.lower())
    words = [i for i, p in enumerate(parts) if p and p not in " —/-"]
    for n, i in enumerate(words):
        if n == 0 or parts[i] not in _SMALL_WORDS:
            parts[i] = parts[i][0].upper() + parts[i][1:]
    return "".join(parts)


def _paragraphs(lines: list[tuple[int, str]]) -> list[tuple[int, int, str]]:
    """Merge wrapped lines into items: (first page, last page, text)."""
    paras: list[list] = []
    for page, line in lines:
        if not paras or _ITEM_START.match(line):
            paras.append([page, page, line])
        else:
            paras[-1][1] = page
            paras[-1][2] += " " + line
    return [(a, b, re.sub(r"\s+", " ", t).strip()) for a, b, t in paras]


def _split_long(text: str, max_chars: int) -> list[str]:
    parts, current = [], ""
    for sentence in re.split(r"(?<=[.;:])\s+", text):
        if current and len(current) + len(sentence) + 1 > max_chars:
            parts.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        parts.append(current)
    return parts


def _pack(paras: list[tuple[int, int, str]], max_chars: int) -> list[tuple[int, int, str]]:
    """Greedily pack items into chunks of at most max_chars (one long item may be split)."""
    pieces = []
    for start, end, text in paras:
        for part in _split_long(text, max_chars) if len(text) > max_chars else [text]:
            pieces.append((start, end, part))

    chunks: list[list] = []
    for start, end, text in pieces:
        if chunks and len(chunks[-1][2]) + len(text) + 1 <= max_chars:
            chunks[-1][1] = end
            chunks[-1][2] += "\n" + text
        else:
            chunks.append([start, end, text])
    return [tuple(c) for c in chunks]


def _lines(pages: list[tuple[int, str]], vocab: Counter) -> list[tuple[int, str]]:
    out = []
    for page_no, text in pages:
        if len(text.strip()) < MIN_PAGE_CHARS:
            continue
        out.extend((page_no, repair_words(raw.strip(), vocab)) for raw in text.splitlines() if raw.strip())
    return out


def chunk_pages(pages: list[tuple[int, str]], max_chars: int = MAX_CHARS) -> list[Chunk]:
    """Turn (page number, text) pairs into Chunks, starting at Rule No. 1."""
    lines = _lines(pages, build_vocab(pages))
    sections: list[tuple[str, str, list[tuple[int, str]]]] = []
    rule = None
    part = ""
    in_comments = False

    def start(rule_name: str, section_name: str) -> None:
        sections.append((rule_name, section_name, []))

    for idx, (page_no, line) in enumerate(lines):
        next_line = lines[idx + 1][1] if idx + 1 < len(lines) else ""
        if m := _RULE.match(line):
            rule, part, in_comments = f"Rule {m.group(1)} — {title_case(m.group(2))}", "", False
            start(rule, "")
            continue
        if rule is None:
            continue  # cover and table of contents
        if _COMMENTS.match(line):
            rule, part, in_comments = "Comments on the Rules", "", True
            start(rule, "")
            continue
        if not in_comments and (m := _PART.match(line)) and _SECTION.match(next_line):
            part = f"{m.group(1)}. {title_case(m.group(2))} — "
            continue
        if not in_comments and (m := _SECTION.match(line)):
            start(rule, f"{part}Section {m.group(1)} — {title_case(m.group(2))}")
            continue
        if in_comments and (m := _COMMENT_HEAD.match(line)):
            start(rule, f"{m.group(1)}. {title_case(m.group(2))}")
            continue
        sections[-1][2].append((page_no, line))

    chunks = []
    for rule_name, section_name, body in sections:
        if not body:
            continue
        for first, last, text in _pack(_paragraphs(body), max_chars):
            chunks.append(Chunk(f"c{len(chunks):04d}", rule_name, section_name, first, last, text))
    return chunks
