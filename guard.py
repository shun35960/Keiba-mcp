"""ガード層。AI から来た買い目を信用せず、投票層に渡す前にすべてここで検証する。

拒否理由は AI が読んで次の行動を決められる文言にする。
ただし上限額・停止ラインなどの設定値そのものは返さない（AI からは読めない設定のため）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from config import GuardConfig
from db import Database
from models import BetType, Race, combination_key, normalize_horses, parse_bet_type, parse_race_id, week_start

# 発走時刻の何分前で締め切るか
CLOSE_BEFORE_POST = timedelta(minutes=2)


class GuardRejection(Exception):
    """ガード層で拒否した。args[0] が AI 向けの拒否理由。"""

    @property
    def reason(self) -> str:
        return str(self.args[0])


@dataclass(frozen=True)
class ValidatedBet:
    race: Race
    bet_type: BetType
    horses: tuple[int, ...]
    combination: str
    amount: int
    week_start: str


class Guard:
    def __init__(self, config: GuardConfig, db: Database):
        self.config = config
        self.db = db

    def check_halt(self) -> None:
        """停止中なら拒否。損失（最後の補充以降）が停止ラインに達していれば停止状態にしてから拒否する。

        停止は人間が補充（admin.py resume）するまで続く。
        """
        if self.db.halted_reason() is None and self.db.loss_since_replenish() >= self.config.loss_stop_threshold:
            self.db.set_halted("累計損失が停止ラインに達したため自動停止")
        reason = self.db.halted_reason()
        if reason is not None:
            if "確認できない" in reason:
                raise GuardRejection(
                    "成立を確認できない投票があるため、投票は自動停止中です。人間が即PAT の投票履歴を確認して再開するまで、投票はすべて拒否されます。"
                )
            raise GuardRejection(
                "累計損失が停止ラインに達したため、投票は自動停止中です。人間が補充して再開するまで、投票はすべて拒否されます。"
            )

    def validate(
        self,
        *,
        race_id: Any,
        bet_type: Any,
        horses: Any,
        amount: Any,
        race: Race | None,
        now: datetime,
    ) -> ValidatedBet:
        """買い目を検証する。問題があれば GuardRejection を送出する。

        DB の集計値を使うため、呼び出し側は Database.transaction() の中で呼び、
        受理した買い目の記録まで同じトランザクションで行うこと。
        """
        cfg = self.config

        # 1. 停止状態（最優先）
        self.check_halt()

        # 2. レース
        try:
            parse_race_id(race_id)
        except ValueError as e:
            raise GuardRejection(str(e)) from None
        if race is None:
            raise GuardRejection(f"race_id '{race_id}' のレースは存在しません。get_race_card で race_id を確認してください。")
        if cfg.allowed_race_numbers and race.number not in cfg.allowed_race_numbers:
            allowed = "、".join(f"{n}R" for n in sorted(cfg.allowed_race_numbers))
            raise GuardRejection(f"{race.number}R は投票対象外です。投票できるのは {allowed} だけです。")
        if now >= race.post_time - CLOSE_BEFORE_POST:
            raise GuardRejection(f"{race.race_id} は締切済みです（発走 {race.post_time:%m/%d %H:%M}）。")

        # 3. 券種
        try:
            bt = parse_bet_type(bet_type)
        except ValueError as e:
            raise GuardRejection(str(e)) from None

        # 4. 馬番
        if not isinstance(horses, (list, tuple)) or not all(
            isinstance(h, int) and not isinstance(h, bool) for h in horses
        ):
            raise GuardRejection("horses は馬番（整数）の配列で指定してください。")
        if len(horses) != bt.size:
            raise GuardRejection(f"{bt.label}は馬番を {bt.size} 頭指定してください（指定: {len(horses)} 頭）。")
        if len(set(horses)) != len(horses):
            raise GuardRejection("同じ馬番が重複しています。異なる馬番を指定してください。")
        runners = race.runner_numbers()
        missing = [h for h in horses if h not in runners]
        if missing:
            raise GuardRejection(
                f"馬番 {missing} は {race.race_id} に出走しません（存在しない・取消・除外）。"
                f"出走馬番: {sorted(runners)}"
            )
        normalized = normalize_horses(bt, horses)
        combination = combination_key(bt, normalized)

        # 5. 金額
        if not isinstance(amount, int) or isinstance(amount, bool):
            raise GuardRejection("amount は円単位の整数で指定してください。")
        if amount < cfg.bet_unit or amount % cfg.bet_unit != 0:
            raise GuardRejection(f"金額は {cfg.bet_unit} 円単位で、{cfg.bet_unit} 円以上を指定してください。")

        # 6. 重複
        if self.db.has_duplicate(race.race_id, bt.code, combination):
            raise GuardRejection(f"{race.race_id} の {bt.label} {combination} はすでに投票済みです（同一買い目の重複は不可）。")

        # 7. 上限額（設定値そのものは返さない）
        if self.db.race_total(race.race_id) + amount > cfg.per_race_limit:
            raise GuardRejection(f"{race.race_id} の1レースあたり上限額を超えるため拒否しました。金額を減らすか見送ってください。")
        ws = week_start(race.date).isoformat()
        if self.db.week_total(ws) + amount > cfg.weekly_limit:
            raise GuardRejection("今週（火〜月）の上限額を超えるため拒否しました。金額を減らすか見送ってください。")

        # 8. 損失の停止ライン：この投票が全額外れても停止ラインを超えないこと
        if self.db.loss_since_replenish() + amount > cfg.loss_stop_threshold:
            raise GuardRejection("この投票が外れると累計損失が停止ラインを超えるため拒否しました。")

        return ValidatedBet(
            race=race,
            bet_type=bt,
            horses=normalized,
            combination=combination,
            amount=amount,
            week_start=ws,
        )
