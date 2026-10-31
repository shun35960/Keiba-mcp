"""結果取得・精算と、MCP サーバとしての動作確認。"""

from __future__ import annotations

import pytest

from mcp.client.client import Client
from server import create_server
from tests.conftest import SAT_MAIN, runners


def test_result_settles_bets(make_service):
    service = make_service(per_race_limit=100000, weekly_limit=100000)
    res = service.data.get_result(SAT_MAIN)
    winner = res.order[0]
    loser = next(n for n in runners(service, SAT_MAIN) if n not in res.order[:3])
    assert service.place_bet(SAT_MAIN, "win", [winner], 300)["accepted"]
    assert service.place_bet(SAT_MAIN, "place", [loser], 200)["accepted"]

    out = service.get_race_result(SAT_MAIN)
    assert out["status"] == "確定"
    assert out["order"][0] == winner
    by_type = {b["bet_type"]: b for b in out["my_bets"]}
    expected = 300 * res.payouts["win"][str(winner)] // 100
    assert by_type["win"]["payout"] == expected and by_type["win"]["hit"]
    assert by_type["place"]["payout"] == 0 and not by_type["place"]["hit"]
    assert out["race_summary"] == {"staked": 500, "returned": expected, "profit": expected - 500}
    assert service.db.cumulative_loss() == 500 - expected


def test_settlement_is_idempotent(service):
    assert service.place_bet(SAT_MAIN, "win", [1], 100)["accepted"]
    first = service.get_race_result(SAT_MAIN)
    second = service.get_race_result(SAT_MAIN)
    assert first["my_bets"] == second["my_bets"]
    assert first["race_summary"] == second["race_summary"]


def test_result_pending_before_race(make_service):
    service = make_service(instant_results=False)
    assert service.place_bet(SAT_MAIN, "win", [1], 100)["accepted"]
    out = service.get_race_result(SAT_MAIN)
    assert out["status"] == "未確定"
    assert service.db.unsettled_bets(SAT_MAIN)


def test_result_for_unknown_race_raises(service):
    with pytest.raises(ValueError):
        service.get_race_result("20261107-NAKAYAMA-11")


def test_get_odds_default_and_combination(service):
    out = service.get_odds(SAT_MAIN)
    assert set(out["odds"]) == {"win", "place"}
    tri = service.get_odds(SAT_MAIN, "3連単", limit=10)
    assert len(tri["odds"]["trifecta"]) == 10
    assert "trifecta" in tri["truncated"]
    values = list(tri["odds"]["trifecta"].values())
    assert values == sorted(values)


def test_get_race_card_rejects_bad_date(service):
    with pytest.raises(ValueError):
        service.get_race_card("2026/11/07")


# ---------------------------------------------------------------- MCP 経由


@pytest.mark.anyio
async def test_mcp_tools_end_to_end(service):
    server = create_server(service)
    async with Client(server) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        assert tools == {"get_race_card", "get_odds", "place_bet", "get_race_result"}

        card = await client.call_tool("get_race_card", {"date": "2026-11-07", "course": "TOKYO"})
        assert not card.is_error
        assert len(card.structured_content["races"]) == 12

        placed = await client.call_tool(
            "place_bet", {"race_id": SAT_MAIN, "bet_type": "win", "horses": [1], "amount": 100}
        )
        assert placed.structured_content["accepted"] is True

        rejected = await client.call_tool(
            "place_bet", {"race_id": SAT_MAIN, "bet_type": "win", "horses": [1], "amount": 100}
        )
        assert rejected.structured_content["accepted"] is False

        result = await client.call_tool("get_race_result", {"race_id": SAT_MAIN})
        assert result.structured_content["status"] == "確定"

        bad = await client.call_tool("get_odds", {"race_id": "nope"})
        assert bad.is_error


@pytest.fixture
def anyio_backend():
    return "asyncio"
