"""The golden question set and how its expected answers are resolved.

Stats and schedule answers are written as SQL against the warehouse and
resolved at eval time, so they can't drift from the data the agent sees.
Rules answers are written by hand from the rulebook text, with the facts an
answer must contain and the rule it should cite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from nba_agent.mcp_server.warehouse import connect, run_query

DEFAULT_GOLDEN_PATH = Path(__file__).with_name("golden.yaml")
CATEGORIES = ("stats", "schedule", "rules", "play_by_play")
KINDS = ("number", "text", "rule")


@dataclass(frozen=True)
class Question:
    id: str
    category: str
    question: str
    kind: str  # number | text | rule
    sql: str | None = None  # resolves the expected value (number/text)
    value: Any = None  # literal expected value when there's no SQL
    tolerance: float = 0.0  # absolute, for numbers
    must_include: tuple[str, ...] = ()  # rule answers: every phrase must appear (case-insensitive)
    any_of: tuple[tuple[str, ...], ...] = ()  # rule answers: one phrase from each group must appear
    cite_rule: int | None = None  # rule answers: must mention "Rule N"
    notes: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


def load_golden(path: Path = DEFAULT_GOLDEN_PATH) -> list[Question]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    questions = []
    for item in raw["questions"]:
        q = Question(
            id=item["id"],
            category=item["category"],
            question=item["question"].strip(),
            kind=item["kind"],
            sql=item.get("sql"),
            value=item.get("value"),
            tolerance=float(item.get("tolerance", 0.0)),
            must_include=tuple(item.get("must_include", ())),
            any_of=tuple(tuple(group) for group in item.get("any_of", ())),
            cite_rule=item.get("cite_rule"),
            notes=item.get("notes", ""),
            tags=tuple(item.get("tags", ())),
        )
        validate(q)
        questions.append(q)
    ids = [q.id for q in questions]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate question ids in golden set")
    return questions


def validate(q: Question) -> None:
    if q.category not in CATEGORIES:
        raise ValueError(f"{q.id}: unknown category {q.category!r}")
    if q.kind not in KINDS:
        raise ValueError(f"{q.id}: unknown kind {q.kind!r}")
    if q.kind in ("number", "text") and (q.sql is None) == (q.value is None):
        raise ValueError(f"{q.id}: number/text questions need exactly one of sql or value")
    if q.kind == "rule" and not (q.must_include or q.any_of):
        raise ValueError(f"{q.id}: rule questions need must_include or any_of")


def resolve_expected(q: Question, db_path: Path | None = None) -> Any:
    """The expected answer: the literal value, or the single cell the SQL returns."""
    if q.kind == "rule":
        return None
    if q.sql is None:
        return q.value
    with connect(db_path) as con:
        # Golden SQL is written by us, not the agent, so it may read staging tables.
        result = run_query(con, q.sql, guarded=False)
    if result.row_count != 1 or len(result.columns) != 1:
        raise ValueError(f"{q.id}: expected SQL to return one cell, got {result.row_count}x{len(result.columns)}")
    return result.rows[0][0]
