"""投票のインターフェース。ガード層を通過した買い目だけがここに渡る。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from guard import ValidatedBet


@dataclass(frozen=True)
class VoteReceipt:
    receipt: str  # 受付番号
    dry_run: bool
    # False: 購入の送信はしたが、成立したかを確認できなかった（人間が投票履歴を確認する必要がある）
    confirmed: bool = True


class VoteError(Exception):
    """投票しなかった（購入の送信前に中止した）。args[0] は AI・人間に見せてよい理由（認証情報を含めない）。

    購入を送信した後は、この例外を出してはいけない（記録が消えて二重購入の原因になるため）。
    送信後に結果が分からないときは VoteReceipt(confirmed=False) を返す。
    """

    @property
    def reason(self) -> str:
        return str(self.args[0])


class Voter(ABC):
    @abstractmethod
    def vote(self, bet: ValidatedBet) -> VoteReceipt:
        """投票する。送信前の失敗は VoteError（買い目は受理として記録されない）。"""
