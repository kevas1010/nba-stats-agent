"""Deterministic graders: no LLM judge, so grading is free, fast and repeatable.

The agent is asked to end with a line `ANSWER: <short answer>`. Numbers and
names are checked against that line (falling back to the whole reply if the
line is missing, which is also recorded). Rules answers are checked for
required facts and the right rule citation anywhere in the reply.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from nba_agent.evals.golden import Question

ANSWER_INSTRUCTION = (
    "\n\nAfter your explanation, end with one line in the form `ANSWER: <short answer>` "
    "containing just the final number, name, or one-sentence ruling."
)
# Matches "ANSWER: x", "**ANSWER:** x" and "**ANSWER**: x" at the start of a line.
_ANSWER_LINE = re.compile(r"^\s*\**ANSWER\**\s*:\s*\**\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_NUMBER = re.compile(r"(?<![\w.])[-−]?\d[\d,]*(?:\.\d+)?%?")


@dataclass(frozen=True)
class Grade:
    correct: bool
    has_answer_line: bool
    extracted: str
    reason: str


def answer_line(text: str) -> str | None:
    matches = _ANSWER_LINE.findall(text or "")
    return matches[-1].strip().strip("`") if matches else None


def normalize(text: str) -> str:
    """Lowercase, strip accents and punctuation: 'Nikola Jokić' -> 'nikola jokic'."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def numbers_in(text: str) -> list[float]:
    """Numbers in the text; a percentage also counts as its 0-1 fraction."""
    out = []
    for token in _NUMBER.findall(text):
        pct = token.endswith("%")
        value = float(token.rstrip("%").replace(",", "").replace("−", "-"))
        out.append(value)
        if pct:
            out.append(value / 100)
    return out


def _grade_number(expected: Any, text: str, tolerance: float) -> tuple[bool, str]:
    target = float(expected)
    found = numbers_in(text)
    # Accept a fraction written as a percentage (0.391 vs "39.1%") as well.
    candidates = {target, target * 100} if abs(target) <= 1 else {target}
    tol = max(tolerance, 1e-9)
    for n in found:
        if any(abs(n - c) <= tol * (100 if c != target else 1) for c in candidates):
            return True, f"found {n:g}"
    return False, f"expected {target:g} (±{tolerance:g}), found {[round(n, 4) for n in found][:6]}"


def _grade_text(expected: Any, text: str) -> tuple[bool, str]:
    want = normalize(str(expected))
    got = normalize(text)
    if want and re.search(rf"\b{re.escape(want)}\b", got):
        return True, f"found {expected!r}"
    # Accept a last name alone when the expected value is a full name.
    last = want.split()[-1] if want else ""
    if len(want.split()) > 1 and len(last) > 3 and re.search(rf"\b{re.escape(last)}\b", got):
        return True, f"found last name {last!r}"
    return False, f"expected {expected!r}"


def _grade_rule(q: Question, text: str) -> tuple[bool, str]:
    got = normalize(text)
    missing = [p for p in q.must_include if normalize(p) not in got]
    missing += ["one of " + "/".join(g) for g in q.any_of if not any(normalize(p) in got for p in g)]
    if q.cite_rule is not None and not re.search(rf"\brule\s*(no\s*)?{q.cite_rule}\b", got):
        missing.append(f"Rule {q.cite_rule} citation")
    return (not missing), ("all facts present" if not missing else "missing: " + "; ".join(missing))


def grade(q: Question, expected: Any, reply: str) -> Grade:
    line = answer_line(reply)
    if q.kind == "rule":
        ok, reason = _grade_rule(q, reply)
        return Grade(ok, line is not None, line or "", reason)
    target_text = line if line is not None else (reply or "")
    if q.kind == "number":
        ok, reason = _grade_number(expected, target_text, q.tolerance)
    else:
        ok, reason = _grade_text(expected, target_text)
    if line is None:
        reason += " (no ANSWER line; graded whole reply)"
    return Grade(ok, line is not None, target_text[:300], reason)
