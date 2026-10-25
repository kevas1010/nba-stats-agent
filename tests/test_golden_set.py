from collections import Counter

import sqlglot

from nba_agent.evals.golden import load_golden


def test_golden_set_loads_and_is_balanced():
    questions = load_golden()
    counts = Counter(q.category for q in questions)
    assert len(questions) == 50
    assert counts == {"stats": 26, "play_by_play": 4, "schedule": 6, "rules": 14}
    assert len({q.question for q in questions}) == len(questions), "duplicate question text"


def test_every_expected_sql_parses_as_one_select():
    for q in load_golden():
        if q.sql:
            statements = [s for s in sqlglot.parse(q.sql, read="duckdb") if s is not None]
            assert len(statements) == 1, q.id
            assert statements[0].key in ("select", "union"), q.id


def test_rules_questions_name_a_rule_or_say_why_not():
    for q in load_golden():
        if q.kind == "rule":
            assert q.cite_rule is not None or "No citation required" in q.notes, q.id
            assert q.notes, f"{q.id}: note where in the rulebook the answer comes from"
