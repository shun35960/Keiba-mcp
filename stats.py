"""ドライランの成績集計（回収率・的中率・収支）。

精算済みの買い目だけを集計する。出走取消・競走除外による返還は「買わなかった」扱いで集計から外す。
設定値（上限額・停止ライン）はここに含めない。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from db import Database
from models import parse_bet_type

# (下限, 上限, 表示名)。下限以上・上限未満
ODDS_BANDS: list[tuple[float | None, float | None, str]] = [
    (None, 2.0, "1.9倍以下"),
    (2.0, 5.0, "2.0〜4.9倍"),
    (5.0, 10.0, "5.0〜9.9倍"),
    (10.0, 30.0, "10〜29.9倍"),
    (30.0, 100.0, "30〜99.9倍"),
    (100.0, None, "100倍以上"),
]


def _rate(num: float, den: float) -> float | None:
    return round(num / den * 100, 1) if den else None


def summarize(rows: list[sqlite3.Row]) -> dict[str, Any]:
    """精算済み（返還を除く）の買い目を集計する。"""
    staked = sum(r["amount"] for r in rows)
    returned = sum(r["payout"] for r in rows)
    hits = sum(1 for r in rows if r["payout"] > 0)
    return {
        "bets": len(rows),
        "races": len({r["race_id"] for r in rows}),
        "hits": hits,
        "staked": staked,
        "returned": returned,
        "profit": returned - staked,
        "recovery_rate": _rate(returned, staked),  # 回収率（%）= 払戻 ÷ 投票額
        "hit_rate": _rate(hits, len(rows)),  # 的中率（%）= 的中した買い目 ÷ 買い目
    }


def _max_drawdown(rows: list[sqlite3.Row]) -> int:
    """累積収支の最大下落幅（精算済みをレース日・投票順に並べたとき）。"""
    peak = 0
    running = 0
    worst = 0
    for r in rows:
        running += r["payout"] - r["amount"]
        peak = max(peak, running)
        worst = max(worst, peak - running)
    return worst


def compute_stats(db: Database, since: str | None = None, until: str | None = None, recent: int = 10) -> dict[str, Any]:
    rows = db.accepted_bets(since, until)
    settled = [r for r in rows if r["payout"] is not None and not r["refunded"]]
    pending = [r for r in rows if r["payout"] is None]
    refunded = [r for r in rows if r["payout"] is not None and r["refunded"]]

    by_type: dict[str, dict[str, Any]] = {}
    for code in dict.fromkeys(r["bet_type"] for r in settled):
        label = parse_bet_type(code).label
        by_type[label] = summarize([r for r in settled if r["bet_type"] == code])

    by_week = [
        {"week_start": w, **summarize([r for r in settled if r["week_start"] == w])}
        for w in sorted({r["week_start"] for r in settled})
    ]

    by_odds: list[dict[str, Any]] = []
    with_odds = [r for r in settled if r["odds_at_bet"] is not None]
    for low, high, label in ODDS_BANDS:
        band = [
            r
            for r in with_odds
            if (low is None or r["odds_at_bet"] >= low) and (high is None or r["odds_at_bet"] < high)
        ]
        if band:
            by_odds.append({"odds_band": label, **summarize(band)})

    recent_rows = sorted(rows, key=lambda r: r["id"], reverse=True)[:recent]
    return {
        "period": {"since": since, "until": until},
        "overall": {**summarize(settled), "max_drawdown": _max_drawdown(settled)},
        "pending": {
            "bets": len(pending),
            "amount": sum(r["amount"] for r in pending),
            "race_ids": sorted({r["race_id"] for r in pending}),
        },
        "refunded": {"bets": len(refunded), "amount": sum(r["amount"] for r in refunded)},
        "by_bet_type": by_type,
        "by_week": by_week,
        "by_odds_at_bet": by_odds,
        "recent_bets": [
            {
                "bet_id": r["id"],
                "race_id": r["race_id"],
                "bet_type": parse_bet_type(r["bet_type"]).label,
                "combination": r["combination"],
                "amount": r["amount"],
                "odds_at_bet": r["odds_at_bet"],
                "payout": r["payout"],
                "status": "未精算" if r["payout"] is None else ("返還" if r["refunded"] else ("的中" if r["payout"] > 0 else "外れ")),
                "note": r["note"],
            }
            for r in recent_rows
        ],
    }
