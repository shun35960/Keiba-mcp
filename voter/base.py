"""投票のインターフェース。ガード層を通過した買い目だけがここに渡る。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from guard import ValidatedBet


@dataclass(frozen=True)
class VoteReceipt:
    receipt: str  # 受付番号
    dry_run: bool


class Voter(ABC):
    @abstractmethod
    def vote(self, bet: ValidatedBet) -> VoteReceipt:
        """投票する。失敗時は例外を送出する（その場合、買い目は受理として記録されない）。"""
