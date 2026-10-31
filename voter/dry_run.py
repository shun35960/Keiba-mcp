"""ドライラン投票。実際には購入せず、受付番号を発行するだけ（記録は server 側で DB に行う）。"""

from __future__ import annotations

import uuid

from guard import ValidatedBet
from voter.base import Voter, VoteReceipt


class DryRunVoter(Voter):
    def vote(self, bet: ValidatedBet) -> VoteReceipt:
        return VoteReceipt(receipt=f"DRY-{uuid.uuid4().hex[:12]}", dry_run=True)
