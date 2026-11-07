"""管理コマンド。MCP ツールではない（AI がツールとして呼ぶことはできない）。

    uv run admin.py status                      # 停止状態・損失・設定値を表示
    uv run admin.py resume --note "3000円補充"   # 補充して投票を再開（確認あり）
    uv run admin.py history                     # 補充の履歴
    uv run admin.py check-credentials           # 認証情報を取り出せるか確認（値は表示しない）
    uv run admin.py check-credentials --netkeiba  # netkeiba の認証情報を確認（ログインはしない）
    uv run admin.py netkeiba-fetch --race 20261107-KYOTO-11   # 重賞の補助データを取得して保存
    uv run admin.py netkeiba-fetch --date 2026-11-07          # その日の重賞をまとめて取得して保存
    uv run admin.py ipat-check --race 20261108-TOKYO-11            # 本物の即PAT で確認画面まで進めてキャンセル（購入しない）
    uv run admin.py ipat-check --race 20261108-TOKYO-11 --types win,trifecta
"""

from __future__ import annotations

import argparse
import sys

from config import RACE_SCOPE_GRADED, load_config
from credentials import (
    CREDENTIAL_LABELS,
    CREDENTIAL_NAMES,
    NETKEIBA_CREDENTIAL_NAMES,
    CredentialError,
    CredentialProvider,
    build_credential_provider,
)
from db import Database


def check_credentials(provider: CredentialProvider, names: tuple[str, ...] = CREDENTIAL_NAMES) -> int:
    """各認証情報を取り出せるかだけを確認する。値・文字数は表示しない。"""
    ok = True
    for name in names:
        label = CREDENTIAL_LABELS[name]
        try:
            provider.get(name)
            print(f"  {label}: 取り出しOK")
        except CredentialError as e:
            ok = False
            print(f"  {label}: NG - {e}")
    print("すべて取り出せました。" if ok else "取り出せないものがあります。")
    return 0 if ok else 1


def ipat_check(cfg, race_id: str, types: str, *, assume_yes: bool = False, voter=None, data=None, now=None) -> int:
    """本物の即PAT で、券種ごとに確認画面まで進めてキャンセルする。購入はしない。"""
    from datetime import datetime

    from guard import CLOSE_BEFORE_POST
    from models import JST, BetType, canonical_race_id, parse_bet_type
    from server import build_data_source
    from voter.ipat_check import build_check_voter, run_check

    try:
        rid = canonical_race_id(race_id)
        bet_types = list(BetType) if types == "all" else [parse_bet_type(t) for t in types.split(",") if t.strip()]
    except ValueError as e:
        print(f"指定が不正です: {e}")
        return 2
    race = (data or build_data_source(cfg)).get_race(rid)
    if race is None:
        print(f"{rid} の出馬表が見つかりません（開催週のレースを指定してください）")
        return 2
    now = now or datetime.now(JST)
    if now >= race.post_time - CLOSE_BEFORE_POST:
        print(f"{rid} は締切済みです（発走 {race.post_time:%m/%d %H:%M}）。発売中のレースを指定してください")
        return 2

    print("本物の即PAT にログインして、次の券種を1つずつ確認画面まで進めます。")
    print("最後の「OK」は押さず、毎回キャンセルします（購入はしません。投票の記録や上限にも影響しません）。")
    print(f"  レース: {rid} {race.name}  券種: {'、'.join(bt.label for bt in bet_types)}  金額: 各100円")
    if not assume_yes:
        if not sys.stdin.isatty():
            print("確認できないため中止しました（--yes を付けると確認を省略します）。")
            return 1
        if input("始めますか？ [y/N]: ").strip().lower() not in ("y", "yes"):
            print("中止しました。")
            return 1

    results = run_check(voter or build_check_voter(cfg), race, bet_types)
    ok = sum(r.ok for r in results)
    print(f"\n結果: {ok}/{len(results)} 券種で確認画面まで進めました（購入はしていません）。")
    for r in results:
        if not r.ok:
            print(f"  NG {r.bet_type.label}: {r.message}")
    if ok < len(results):
        print("失敗した手順の画面（加工済み）は captures/voter/ に保存されています。")
    return 0 if ok == len(results) else 1


def _netkeiba_targets(data, race_id: str | None, race_date: str | None, course: str | None, now) -> list:
    """取得するレース（発走前の重賞）を選ぶ。--race の指定が対象外なら ValueError。"""
    from datetime import date

    from models import GRADED_STAKES, canonical_race_id

    if race_id is not None:
        rid = canonical_race_id(race_id)
        race = data.get_race(rid)
        if race is None:
            raise ValueError(f"{rid} の出馬表が見つかりません（開催週のレースを指定してください）")
        if race.grade not in GRADED_STAKES:
            raise ValueError(f"{rid} は重賞ではないため取得しません（取得できるのは平地 G・障害 J・G だけです）")
        if now >= race.post_time:
            raise ValueError(f"{rid} は発走後のため取得しません（発走後の情報を予想に混ぜないため）")
        return [race]
    races = data.get_races(date.fromisoformat(race_date), course)
    return [r for r in races if r.grade in GRADED_STAKES and now < r.post_time]


def netkeiba_fetch(
    cfg,
    race_id: str | None = None,
    race_date: str | None = None,
    course: str | None = None,
    *,
    save_pages: bool = False,
    data=None,
    provider: CredentialProvider | None = None,
    transport=None,
    sleep=None,
    now=None,
) -> int:
    """netkeiba から重賞の補助データを取得して JSON に保存する。サーバはこのファイルを読むだけ。

    失敗したら残りのレースも取りに行かず、その場で止める（自動で再試行しない）。
    """
    from datetime import datetime
    from pathlib import Path

    from data.netkeiba import NetkeibaError, NetkeibaFetcher, fetch_race_insights
    from data.netkeiba_parse import netkeiba_race_id
    from data.scraper import FetchError, JraDataSource
    from data.supplement import save_insights
    from models import JST
    from server import build_data_source

    data = data or build_data_source(cfg)
    if not isinstance(data, JraDataSource):
        print("netkeiba-fetch は JRA のデータが必要です（config.toml の data.source を \"jra\" にしてください）")
        return 2
    now = now or datetime.now(JST)
    try:
        targets = _netkeiba_targets(data, race_id, race_date, course, now)
        ids = {r.race_id: netkeiba_race_id(data.card_cname(r.race_id) or "", r.race_id) for r in targets}
    except ValueError as e:
        print(f"取得しませんでした: {e}")
        return 2
    except FetchError as e:
        print(f"JRA サイトから出馬表を取得できませんでした: {e}")
        return 1
    if not targets:
        print("取得できるレース（発走前の重賞）はありません。")
        return 0

    provider = provider or build_credential_provider(cfg)
    fetcher = NetkeibaFetcher(
        cfg.netkeiba_cache_path,
        lambda: (provider.get("netkeiba_id"), provider.get("netkeiba_password")),
        min_interval=cfg.netkeiba_request_interval,
        user_agent=cfg.netkeiba_user_agent,
        transport=transport,
        pages_dir=Path(cfg.db_path).parent / "captures" / "netkeiba" if save_pages else None,
        **({"sleep": sleep} if sleep else {}),
    )
    try:
        for race in targets:
            try:
                insights, warnings = fetch_race_insights(fetcher, race, ids[race.race_id])
            except CredentialError as e:
                print(f"netkeiba の認証情報を取り出せないため中止しました（netkeiba には接続していません）: {e}")
                print("Bitwarden Secrets Manager への登録方法は README の「netkeiba の補助データ」を見てください。")
                return 1
            except NetkeibaError as e:
                print(f"{race.race_id}: 取得を中止しました: {e}")
                return 1
            path = save_insights(cfg.insights_dir, insights)
            print(f"{race.race_id} {race.name}: {len(insights.horses)}頭分を {path} に保存しました。")
            for w in warnings:
                print(f"  注意: {w}")
    finally:
        fetcher.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="keiba-mcp 管理コマンド（人間専用）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="停止状態・損失・設定値を表示")
    r = sub.add_parser("resume", help="補充して投票を再開する")
    r.add_argument("--note", help="補充のメモ（例: 3000円補充）")
    r.add_argument("--yes", action="store_true", help="確認を省略する")
    sub.add_parser("history", help="補充の履歴")
    k = sub.add_parser("check-credentials", help="認証情報を取り出せるか確認する（値は表示しない）")
    k.add_argument("--netkeiba", action="store_true", help="即PAT の代わりに netkeiba の認証情報を確認する（ログインはしない）")
    n = sub.add_parser("netkeiba-fetch", help="netkeiba から重賞の補助データを取得して保存する（低頻度で使う）")
    target = n.add_mutually_exclusive_group(required=True)
    target.add_argument("--race", help="race_id（例: 20261107-KYOTO-11）。発走前の重賞を指定する")
    target.add_argument("--date", help="開催日（YYYY-MM-DD）。その日の発走前の重賞をまとめて取得する")
    n.add_argument("--course", help="--date のとき競馬場で絞り込む（例: KYOTO / 京都）")
    n.add_argument("--save-pages", action="store_true", help="取得したページを captures/netkeiba/ に保存する（解析の調整用。外に出さない）")
    c = sub.add_parser("ipat-check", help="本物の即PAT で確認画面まで進めて必ずキャンセルする（購入しない）")
    c.add_argument("--race", required=True, help="race_id（例: 20261108-TOKYO-11）。発売中のレースを指定する")
    c.add_argument("--types", default="all", help="試す券種（例: win,quinella,trifecta）。既定は全7券種")
    c.add_argument("--yes", action="store_true", help="確認を省略する")
    args = ap.parse_args(argv)

    cfg = load_config()
    if args.cmd == "check-credentials":
        names = NETKEIBA_CREDENTIAL_NAMES if args.netkeiba else CREDENTIAL_NAMES
        return check_credentials(build_credential_provider(cfg), names)
    if args.cmd == "netkeiba-fetch":
        return netkeiba_fetch(cfg, args.race, args.date, args.course, save_pages=args.save_pages)
    if args.cmd == "ipat-check":
        return ipat_check(cfg, args.race, args.types, assume_yes=args.yes)

    db = Database(cfg.db_path)
    g = cfg.guard
    try:
        if args.cmd == "status":
            halted = db.halted_reason()
            print(f"投票: {'停止中（' + halted + '）' if halted else '受付中'}")
            print(f"損失（最後の補充以降）: {db.loss_since_replenish():,}円 / 停止ライン {g.loss_stop_threshold:,}円")
            print(f"累計損失（全期間）: {db.cumulative_loss():,}円")
            limit_line = f"上限: 1レース {g.per_race_limit:,}円 / 週 {g.weekly_limit:,}円"
            if g.sunday_reserve > 0:
                limit_line += f"（日曜リザーブ {g.sunday_reserve:,}円）"
            print(limit_line)
            races = "、".join(f"{n}R" for n in sorted(g.allowed_race_numbers)) or "全レース"
            if g.race_scope == RACE_SCOPE_GRADED:
                races += " ＋ 重賞（G・リステッド・J・G）"
            print(f"対象レース: {races}")
            print(f"投票方式: {cfg.voter_mode}")
            return 0

        if args.cmd == "history":
            rows = db.replenishment_history()
            if not rows:
                print("補充の履歴はありません。")
            for row in rows:
                print(f"#{row['id']} {row['at']} 基準の累計損失 {row['loss_baseline']:,}円 {row['note'] or ''}")
            return 0

        # resume
        loss = db.loss_since_replenish()
        print(f"現在の損失（最後の補充以降）: {loss:,}円 / 停止ライン {g.loss_stop_threshold:,}円")
        print("補充すると停止を解除し、ここから新たに停止ラインまで投票できるようになります。")
        if not args.yes:
            if not sys.stdin.isatty():
                print("確認できないため中止しました（--yes を付けると確認を省略します）。")
                return 1
            if input("補充して再開しますか？ [y/N]: ").strip().lower() not in ("y", "yes"):
                print("中止しました。")
                return 1
        baseline = db.replenish(args.note)
        print(f"再開しました（基準の累計損失: {baseline:,}円）。")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
