"""Playwright による実投票（スタブ）。

README の方針により、ドライランでの検証が済んで別途判断されるまで実装しない。
ログイン・購入確定の処理はここに書かないこと。
"""

from __future__ import annotations

from guard import ValidatedBet
from voter.base import Voter, VoteReceipt


class BrowserVoter(Voter):
    def __init__(self, *args, **kwargs):
        raise NotImplementedError("実投票は未実装です（開発フェーズはドライランのみ）。config.toml の voter.mode は 'dry_run' にしてください。")

    def vote(self, bet: ValidatedBet) -> VoteReceipt:
        raise NotImplementedError
