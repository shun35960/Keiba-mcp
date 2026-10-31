"""人間専用の管理コマンド。MCP（AI）からは使えない。

    uv run admin.py status                      # 停止状態・損失・設定値を表示
    uv run admin.py resume --note "3000円補充"   # 補充して投票を再開（確認あり）
    uv run admin.py history                     # 補充の履歴
    uv run admin.py check-credentials           # 認証情報を取り出せるか確認（値は表示しない）
"""

from __future__ import annotations

import argparse
import sys

from config import load_config
from credentials import CREDENTIAL_LABELS, CREDENTIAL_NAMES, CredentialError, CredentialProvider, build_credential_provider
from db import Database


def check_credentials(provider: CredentialProvider) -> int:
    """各認証情報を取り出せるかだけを確認する。値・文字数は表示しない。"""
    ok = True
    for name in CREDENTIAL_NAMES:
        label = CREDENTIAL_LABELS[name]
        try:
            provider.get(name)
            print(f"  {label}: 取り出しOK")
        except CredentialError as e:
            ok = False
            print(f"  {label}: NG - {e}")
    print("すべて取り出せました。" if ok else "取り出せないものがあります。")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="keiba-mcp 管理コマンド（人間専用）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="停止状態・損失・設定値を表示")
    r = sub.add_parser("resume", help="補充して投票を再開する")
    r.add_argument("--note", help="補充のメモ（例: 3000円補充）")
    r.add_argument("--yes", action="store_true", help="確認を省略する")
    sub.add_parser("history", help="補充の履歴")
    sub.add_parser("check-credentials", help="認証情報を取り出せるか確認する（値は表示しない）")
    args = ap.parse_args(argv)

    cfg = load_config()
    if args.cmd == "check-credentials":
        return check_credentials(build_credential_provider(cfg))

    db = Database(cfg.db_path)
    g = cfg.guard
    try:
        if args.cmd == "status":
            halted = db.halted_reason()
            print(f"投票: {'停止中（' + halted + '）' if halted else '受付中'}")
            print(f"損失（最後の補充以降）: {db.loss_since_replenish():,}円 / 停止ライン {g.loss_stop_threshold:,}円")
            print(f"累計損失（全期間）: {db.cumulative_loss():,}円")
            print(f"上限: 1レース {g.per_race_limit:,}円 / 週 {g.weekly_limit:,}円")
            races = "、".join(f"{n}R" for n in sorted(g.allowed_race_numbers)) or "全レース"
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
