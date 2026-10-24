import asyncio

import duckdb
import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from conftest import RULE_CHUNKS, HashEmbedder
from nba_agent.mcp_server.server import RulebookTool, create_server
from nba_agent.rulebook.index import build_index
from nba_agent.rulebook.search import BM25, RuleHit, RulebookSearcher, tokenize


def test_tokenize_drops_stopwords_and_punctuation():
    assert tokenize("How long is the Shot-Clock?") == ["long", "shot", "clock"]


def test_bm25_prefers_documents_with_rare_query_terms():
    bm25 = BM25(["shot clock shot clock", "foul foul foul", "shot foul"])
    scores = bm25.scores("clock")
    assert scores[0] > scores[2] == scores[1] == 0


@pytest.mark.parametrize(
    "hit, citation",
    [
        (RuleHit("c1", "Rule 7 — Shot Clock", "Section I — Definition", 28, 28, "", 0.1),
         "Rule 7 — Shot Clock, Section I — Definition, p. 28"),
        (RuleHit("c2", "Rule 14 — Coach’s Challenge", "Section I — Overview", 60, 61, "", 0.1),
         "Rule 14 — Coach’s Challenge, Section I — Overview, pp. 60-61"),
        (RuleHit("c3", "Comments on the Rules", "", 65, 65, "", 0.1), "Comments on the Rules, p. 65"),
    ],
)
def test_citation_format(hit, citation):
    assert hit.citation == citation


def test_search_finds_the_relevant_rule(rulebook_warehouse):
    with duckdb.connect(str(rulebook_warehouse), read_only=True) as con:
        searcher = RulebookSearcher.load(con, HashEmbedder())
    assert searcher.search("how many seconds on the shot clock", k=1)[0].chunk_id == "c0000"
    assert searcher.search("sixth personal foul disqualified", k=1)[0].chunk_id == "c0001"
    assert searcher.search("can a player hang on the rim", k=1)[0].chunk_id == "c0002"


def test_search_returns_at_most_k_unique_hits(rulebook_warehouse):
    with duckdb.connect(str(rulebook_warehouse), read_only=True) as con:
        hits = RulebookSearcher.load(con, HashEmbedder()).search("player foul ball", k=3)
    assert len(hits) == 3
    assert len({h.chunk_id for h in hits}) == 3
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)


def test_load_refuses_a_different_embedding_model(rulebook_warehouse):
    class OtherModel(HashEmbedder):
        name = "other-model"

    with duckdb.connect(str(rulebook_warehouse), read_only=True) as con:
        with pytest.raises(RuntimeError, match="rebuild with nba-rulebook"):
            RulebookSearcher.load(con, OtherModel())


# --- MCP tool -------------------------------------------------------------------

def test_tool_returns_citations_and_text(rulebook_warehouse):
    result = RulebookTool(rulebook_warehouse, HashEmbedder)("Is a player disqualified after six fouls?", k=2)
    assert result["rulebook"] == "2026-27 Official NBA Playing Rules"
    top = result["results"][0]
    assert top["citation"] == "Rule 3 — Players, Substitutes, and Coaches, Section I — Team, p. 15"
    assert "sixth personal foul" in top["text"]
    assert len(result["results"]) == 2


@pytest.mark.parametrize(
    "query, k, message",
    [
        ("", 5, "must not be empty"),
        ("   ", 5, "must not be empty"),
        ("x" * 501, 5, "at most 500"),
        ("shot clock", 0, "k must be between"),
        ("shot clock", 11, "k must be between"),
    ],
)
def test_tool_validates_arguments(rulebook_warehouse, query, k, message):
    with pytest.raises(ToolError, match=message):
        RulebookTool(rulebook_warehouse, HashEmbedder)(query, k)


def test_tool_reports_missing_index(warehouse):
    with pytest.raises(ToolError, match="run nba-rulebook"):
        RulebookTool(warehouse, HashEmbedder)("shot clock")


def test_tool_loads_the_model_once_and_picks_up_rebuilds(rulebook_warehouse):
    created = []

    def factory():
        created.append(1)
        return HashEmbedder()

    tool = RulebookTool(rulebook_warehouse, factory)
    tool("shot clock")
    tool("coach challenge")
    assert len(created) == 1

    with duckdb.connect(str(rulebook_warehouse)) as con:
        build_index(con, RULE_CHUNKS[:1], HashEmbedder(), season="2026-27", source_url="x")
    assert len(tool("coach challenge", k=10)["results"]) == 1
    assert len(created) == 1


def test_mcp_client_can_search_the_rulebook(rulebook_warehouse):
    async def go():
        async with Client(create_server(rulebook_warehouse, HashEmbedder)) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            result = await client.call_tool("search_rulebook", {"query": "shot clock seconds", "k": 1})
            return tools, result

    tools, result = asyncio.run(go())
    assert tools["search_rulebook"].annotations.read_only_hint
    assert not result.is_error
    assert result.structured_content["results"][0]["citation"].startswith("Rule 7 — Shot Clock")
