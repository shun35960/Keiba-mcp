"""SQLite への記録（買い目・結果・損益・停止状態）。"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from models import JST, RaceResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS bets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT    NOT NULL,
    race_id      TEXT    NOT NULL,
    race_date    TEXT,              -- race_id が不正な拒否記録では NULL
    week_start   TEXT,
    bet_type     TEXT    NOT NULL,
    combination  TEXT    NOT NULL,  -- 正規化した買い目（例: 1-2, 1>2>3）
    amount       INTEGER NOT NULL,
    status       TEXT    NOT NULL CHECK (status IN ('accepted', 'rejected')),
    reason       TEXT,              -- 拒否理由
    receipt      TEXT,              -- 投票層の受付番号
    payout       INTEGER,           -- 精算後の払戻額（外れは0）。未精算は NULL
    settled_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_bets_race ON bets (race_id, status);
CREATE INDEX IF NOT EXISTS idx_bets_week ON bets (week_start, status);

CREATE TABLE IF NOT EXISTS results (
    race_id      TEXT PRIMARY KEY,
    finish_order TEXT NOT NULL,     -- JSON 配列
    payouts      TEXT NOT NULL,     -- JSON
    fetched_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """検証から記録までを1つの排他トランザクションにまとめる（並行した投票で上限を超えないように）。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # ------------------------------------------------------------------ 集計

    def race_total(self, race_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM bets WHERE race_id = ? AND status = 'accepted'",
            (race_id,),
        ).fetchone()
        return int(row[0])

    def week_total(self, week_start: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM bets WHERE week_start = ? AND status = 'accepted'",
            (week_start,),
        ).fetchone()
        return int(row[0])

    def has_duplicate(self, race_id: str, bet_type: str, combination: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM bets WHERE race_id = ? AND bet_type = ? AND combination = ? AND status = 'accepted' LIMIT 1",
            (race_id, bet_type, combination),
        ).fetchone()
        return row is not None

    def cumulative_loss(self) -> int:
        """累計損失 = 受理した投票額の合計 − 払戻の合計。未精算の投票は全額損失として数える。"""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(amount), 0), COALESCE(SUM(payout), 0) FROM bets WHERE status = 'accepted'"
        ).fetchone()
        return int(row[0]) - int(row[1])

    # ------------------------------------------------------------------ 停止状態

    def halted_reason(self) -> str | None:
        row = self._conn.execute("SELECT value FROM state WHERE key = 'halted'").fetchone()
        return None if row is None else str(row[0])

    def set_halted(self, reason: str) -> None:
        self._conn.execute(
            "INSERT INTO state (key, value) VALUES ('halted', ?) ON CONFLICT(key) DO NOTHING",
            (f"{_now()} {reason}",),
        )

    # ------------------------------------------------------------------ 買い目

    def insert_bet(
        self,
        *,
        race_id: str,
        race_date: str | None,
        week_start: str | None,
        bet_type: str,
        combination: str,
        amount: int,
        status: str,
        reason: str | None = None,
        receipt: str | None = None,
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO bets (created_at, race_id, race_date, week_start, bet_type, combination,
                                 amount, status, reason, receipt)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (_now(), race_id, race_date, week_start, bet_type, combination, amount, status, reason, receipt),
        )
        return int(cur.lastrowid)

    def unsettled_bets(self, race_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM bets WHERE race_id = ? AND status = 'accepted' AND payout IS NULL ORDER BY id",
            (race_id,),
        ).fetchall()

    def bets_for_race(self, race_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM bets WHERE race_id = ? AND status = 'accepted' ORDER BY id", (race_id,)
        ).fetchall()

    def settle_bet(self, bet_id: int, payout: int) -> None:
        self._conn.execute(
            "UPDATE bets SET payout = ?, settled_at = ? WHERE id = ? AND payout IS NULL",
            (payout, _now(), bet_id),
        )

    # ------------------------------------------------------------------ 結果

    def save_result(self, result: RaceResult) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO results (race_id, finish_order, payouts, fetched_at) VALUES (?, ?, ?, ?)",
            (result.race_id, json.dumps(list(result.order)), json.dumps(result.payouts, ensure_ascii=False), _now()),
        )
