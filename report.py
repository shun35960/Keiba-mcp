"""ドライラン成績のレポート（人間向け）。

    uv run report.py                     # 全期間
    uv run report.py --since 2026-11-01  # 期間を指定
    uv run report.py --settle            # 先に未精算のレースの結果を取得して精算する
"""

from __future__ import annotations

import argparse
from typing import Any

from config import load_config
from server import build_service


def _yen(v: int) -> str:
    return f"{v:+,}円" if v else "0円"


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v:.1f}%"


def _row(label: str, s: dict[str, Any]) -> str:
    return (
        f"  {label:<12} {s['bets']:>4}点  的中{s['hits']:>3}  投票{s['staked']:>8,}円  払戻{s['returned']:>8,}円  "
        f"収支{_yen(s['profit']):>10}  回収率{_pct(s['recovery_rate']):>7}  的中率{_pct(s['hit_rate']):>6}"
    )


def render(stats: dict[str, Any]) -> str:
    lines: list[str] = []
    p = stats["period"]
    period = "全期間" if not (p["since"] or p["until"]) else f"{p['since'] or '…'} 〜 {p['until'] or '…'}"
    lines.append(f"■ ドライラン成績（{period}）")
    o = stats["overall"]
    lines.append(_row("全体", o))
    lines.append(f"  レース数 {o['races']}  最大ドローダウン {o['max_drawdown']:,}円")
    if stats["pending"]["bets"]:
        lines.append(f"  未精算 {stats['pending']['bets']}点 {stats['pending']['amount']:,}円 {stats['pending']['race_ids']}")
    if stats["refunded"]["bets"]:
        lines.append(f"  返還（集計外） {stats['refunded']['bets']}点 {stats['refunded']['amount']:,}円")
    if stats.get("betting_halted"):
        lines.append("  ※ 累計損失が停止ラインに達し、投票は停止中です")

    if stats["by_bet_type"]:
        lines.append("\n■ 券種別")
        lines += [_row(k, v) for k, v in stats["by_bet_type"].items()]
    if stats["by_week"]:
        lines.append("\n■ 週別（火曜始まり）")
        lines += [_row(w["week_start"], w) for w in stats["by_week"]]
    if stats["by_odds_at_bet"]:
        lines.append("\n■ 投票時オッズ帯別")
        lines += [_row(b["odds_band"], b) for b in stats["by_odds_at_bet"]]
    if stats["recent_bets"]:
        lines.append("\n■ 直近の買い目")
        for b in stats["recent_bets"]:
            odds = "-" if b["odds_at_bet"] is None else f"{b['odds_at_bet']}倍"
            payout = "" if b["payout"] is None else f" 払戻{b['payout']:,}円"
            lines.append(
                f"  #{b['bet_id']} {b['race_id']} {b['bet_type']} {b['combination']} {b['amount']:,}円 ({odds}) "
                f"{b['status']}{payout}"
            )
            if b["note"]:
                lines.append(f"      理由: {b['note']}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="ドライラン成績のレポート")
    ap.add_argument("--since", help="集計開始日 YYYY-MM-DD")
    ap.add_argument("--until", help="集計終了日 YYYY-MM-DD")
    ap.add_argument("--settle", action="store_true", help="先に未精算のレースの結果を取得して精算する")
    args = ap.parse_args()

    service = build_service(load_config())
    if args.settle:
        out = service.settle_pending_bets()
        for s in out["settled"]:
            print(f"精算: {s['race_id']} 収支 {_yen(s['profit'])}")
        for w in out["waiting"]:
            print(f"保留: {w['race_id']} {w['status']}")
        print()
    print(render(service.get_stats(args.since, args.until)))


if __name__ == "__main__":
    main()
