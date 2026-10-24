from collections import Counter

import pytest

from nba_agent.rulebook.chunk import Chunk, chunk_pages, repair_words, title_case

FILLER = "Filler sentence that keeps this page above the minimum length for rule pages. " * 8

VOCAB = Counter({
    "penalties": 40, "penal": 1, "ties": 3,
    "technical": 50, "echnical": 1, "t": 20, "no": 100,
    "a": 900, "way": 2, "away": 11,
    "the": 800, "re": 2, "there": 5,
    "in": 500, "to": 600, "into": 30,
    "players": 80, "pla": 1, "yers": 1,
    "fan": 1, "f": 30, "an": 200,
})


@pytest.mark.parametrize(
    "raw, fixed",
    [
        ("RULE NO. 9—FREE THROWS AND PENAL TIES", "RULE NO. 9—FREE THROWS AND PENALTIES"),
        ("PLA YERS, SUBSTITUTES", "PLAYERS, SUBSTITUTES"),
        ("no T echnical foul", "no Technical foul"),
        ("M. VERBAL F AN INTERFERENCE", "M. VERBAL FAN INTERFERENCE"),
        # Real word pairs stay apart.
        ("the ball in such a way that", "the ball in such a way that"),
        ("passed in to the post", "passed in to the post"),
        ("results in the re-routing of", "results in the re-routing of"),
    ],
)
def test_repair_words(raw, fixed):
    assert repair_words(raw, VOCAB) == fixed


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("SHOT CLOCK", "Shot Clock"),
        ("COACH’S CHALLENGE", "Coach’s Challenge"),
        ("FOULS AND PENALTIES", "Fouls and Penalties"),
        ("OFFICIALS AND THEIR DUTIES", "Officials and Their Duties"),
        ("BASKET INTERFERENCE—GOALTENDING", "Basket Interference—Goaltending"),
        ("Starting and Stopping of Shot Clock", "Starting and Stopping of Shot Clock"),
    ],
)
def test_title_case(raw, expected):
    assert title_case(raw) == expected


def _book() -> list[tuple[int, str]]:
    toc = "Rule No. 7 – Shot Clock 28\n Section I – Definition 28\n" + FILLER
    rule7 = (
        "RULE NO. 7—SHOT CLOCK\n"
        "Section I—Definition\n"
        "a. The shot clock shall be 24 seconds.\n"
        "b. A team must attempt a field goal\n"
        "within the time allowed.\n"
        "Section II—Starting and Stopping of Shot Clock\n"
        "a. The shot clock starts when a player gains possession.\n" + FILLER
    )
    rule12 = (
        "RULE NO. 12—FOULS AND PENALTIES\n"
        "A.   Technical Foul\n"
        "Section I—Excessive Timeouts\n"
        "a. Requests for a timeout in excess of the authorized number are granted.\n"
        "B.   Personal Foul\n"
        "Section I—Types\n"
        "a. A player shall not hold, push or charge an opponent.\n" + FILLER
    )
    comments = (
        "COMMENTS ON THE RULES\n"
        "A. CONTACT SITUATIONS\n"
        "Contact which is incidental shall be ignored.\n" + FILLER
    )
    signals = "TRAVELING\nCHARGING"  # signal-chart page: too short to index
    return [(3, toc), (28, rule7), (39, rule12), (65, comments), (72, signals)]


def test_chunks_follow_rules_and_sections():
    headings = [c.heading for c in chunk_pages(_book())]
    assert headings[0] == "Rule 7 — Shot Clock > Section I — Definition"
    assert "Rule 7 — Shot Clock > Section II — Starting and Stopping of Shot Clock" in headings
    assert "Comments on the Rules > A. Contact Situations" in headings


def test_table_of_contents_is_skipped():
    assert all(c.page_start >= 28 for c in chunk_pages(_book()))


def test_rule_12_parts_keep_restarted_sections_distinct():
    headings = {c.heading for c in chunk_pages(_book())}
    assert "Rule 12 — Fouls and Penalties > A. Technical Foul — Section I — Excessive Timeouts" in headings
    assert "Rule 12 — Fouls and Penalties > B. Personal Foul — Section I — Types" in headings


def test_wrapped_lines_are_merged_into_one_item():
    first = chunk_pages(_book())[0]
    assert "b. A team must attempt a field goal within the time allowed." in first.text
    assert first.page_start == first.page_end == 28


def test_short_pages_are_dropped():
    assert not any("CHARGING" in c.text for c in chunk_pages(_book()))


def test_chunks_respect_max_chars_and_have_unique_ids():
    pages = [(10, "RULE NO. 1—COURT\nSection I—Court\n" + "\n".join(f"{i}. " + "word " * 60 for i in range(1, 30)))]
    chunks = chunk_pages(pages, max_chars=400)
    assert len(chunks) > 5
    assert all(len(c.text) <= 400 for c in chunks)
    assert len({c.chunk_id for c in chunks}) == len(chunks)


def test_embedding_text_includes_heading():
    chunk = Chunk("c0001", "Rule 7 — Shot Clock", "Section I — Definition", 28, 28, "a. 24 seconds.")
    assert chunk.embedding_text() == "Rule 7 — Shot Clock > Section I — Definition\na. 24 seconds."
