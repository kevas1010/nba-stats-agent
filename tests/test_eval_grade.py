import pytest

from nba_agent.evals.golden import Question, resolve_expected, validate
from nba_agent.evals.grade import answer_line, grade, normalize, numbers_in


def q(kind="number", **kw):
    return Question(id="t", category="stats", question="?", kind=kind, value=kw.pop("value", 1), **kw)


def test_answer_line_takes_the_last_one():
    reply = "Thinking aloud. ANSWER: not this\nMore text.\n**ANSWER:** 51 wins"
    assert answer_line(reply) == "51 wins"
    assert answer_line("no final line here") is None


def test_numbers_in_handles_commas_signs_and_percents():
    assert numbers_in("1,230 games, -4.5 net, 39.1% from three") == [1230.0, -4.5, 39.1, 0.391]


def test_normalize_strips_accents_and_punctuation():
    assert normalize("Nikola Jokić!") == "nikola jokic"


@pytest.mark.parametrize(
    "expected, reply, tol, ok",
    [
        (51, "They went 51-31.\nANSWER: 51", 0, True),
        (51, "ANSWER: 50", 0, False),
        (99.1, "ANSWER: about 99.1 possessions per 48", 0.05, True),
        (99.12, "ANSWER: 99.1", 0.05, True),
        (0.391, "ANSWER: 39.1%", 0.001, True),
        (0.391, "ANSWER: .391", 0.001, False),  # leading-dot decimals aren't parsed; prompt asks for plain numbers
        (12, "They won 12 games", 0, True),  # no ANSWER line: falls back to whole reply
    ],
)
def test_number_grading(expected, reply, tol, ok):
    assert grade(q(tolerance=tol), expected, reply).correct is ok


def test_missing_answer_line_is_recorded():
    g = grade(q(), 12, "They won 12 games")
    assert g.correct and not g.has_answer_line and "no ANSWER line" in g.reason


@pytest.mark.parametrize(
    "expected, reply, ok",
    [
        ("Nikola Jokić", "ANSWER: Nikola Jokic", True),
        ("Nikola Jokić", "ANSWER: Jokić", True),  # last name alone is enough
        ("Shai Gilgeous-Alexander", "ANSWER: Gilgeous-Alexander", True),
        ("Luka Dončić", "ANSWER: Anthony Edwards", False),
        ("NYK", "ANSWER: NYK, by 6 games", True),
        ("BOS", "ANSWER: NYK", False),
    ],
)
def test_text_grading(expected, reply, ok):
    assert grade(q(kind="text", value=expected), expected, reply).correct is ok


def test_rule_grading_needs_facts_and_citation():
    rq = Question(id="r", category="rules", question="?", kind="rule",
                  must_include=("14",), any_of=(("offensive rebound", "kicked"),), cite_rule=7)
    good = "Rule 7, Section IV: after an offensive rebound off the rim the clock resets to 14.\nANSWER: 14 seconds"
    assert grade(rq, None, good).correct
    no_cite = grade(rq, None, "It resets to 14 after an offensive rebound.")
    assert not no_cite.correct and "Rule 7 citation" in no_cite.reason
    assert not grade(rq, None, "Rule 7 says 24 seconds.").correct
    assert not grade(rq, None, "Rule 17: 14 after an offensive rebound.").correct  # Rule 17 isn't Rule 7


@pytest.mark.parametrize(
    "kw, message",
    [
        (dict(category="golf"), "unknown category"),
        (dict(kind="essay"), "unknown kind"),
        (dict(kind="number", value=None), "exactly one of sql or value"),
        (dict(kind="number", value=1, sql="select 1"), "exactly one of sql or value"),
        (dict(kind="rule", value=None), "must_include or any_of"),
    ],
)
def test_validate_rejects_bad_questions(kw, message):
    base = dict(id="x", category="stats", question="?", kind="number", value=1)
    base.update(kw)
    with pytest.raises(ValueError, match=message):
        validate(Question(**base))


def test_resolve_expected_from_sql(warehouse):
    sql_q = Question(id="s", category="stats", question="?", kind="number",
                     sql="select avg(pace) from marts.team_pace where team = 'NYK'")
    assert resolve_expected(sql_q, warehouse) == 96.5
    multi = Question(id="m", category="stats", question="?", kind="number", sql="select pace from marts.team_pace")
    with pytest.raises(ValueError, match="one cell"):
        resolve_expected(multi, warehouse)
