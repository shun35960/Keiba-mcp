from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from config import AppConfig, GuardConfig
from data.dummy import DummyDataSource
from db import Database
from models import JST
from server import KeibaService
from voter.dry_run import DryRunVoter

# 2026-11-07 は土曜、11-08 は日曜、11-09 は月曜
SAT = "20261107"
SUN = "20261108"
SAT_MAIN = f"{SAT}-TOKYO-11"
SAT_MAIN_KYOTO = f"{SAT}-KYOTO-11"
SUN_MAIN = f"{SUN}-TOKYO-11"
SAT_MORNING = datetime(2026, 11, 7, 9, 0, tzinfo=JST)


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def make_config(tmp_path: Path, **guard_overrides) -> AppConfig:
    guard = GuardConfig(
        per_race_limit=1000,
        weekly_limit=1000,
        loss_stop_threshold=10000,
        bet_unit=100,
        allowed_race_numbers=frozenset({11}),
    )
    return AppConfig(
        guard=replace(guard, **guard_overrides),
        data_source="dummy",
        dummy_instant_results=True,
        voter_mode="dry_run",
        db_path=tmp_path / "test.sqlite",
    )


@pytest.fixture
def clock() -> Clock:
    return Clock(SAT_MORNING)


@pytest.fixture
def make_service(tmp_path, clock):
    created: list[KeibaService] = []

    def _make(*, voter=None, instant_results=True, **guard_overrides) -> KeibaService:
        cfg = make_config(tmp_path, **guard_overrides)
        svc = KeibaService(
            config=cfg,
            data=DummyDataSource(instant_results=instant_results, clock=clock),
            voter=voter or DryRunVoter(),
            db=Database(cfg.db_path),
            clock=clock,
        )
        created.append(svc)
        return svc

    yield _make
    for s in created:
        s.db.close()


@pytest.fixture
def service(make_service) -> KeibaService:
    return make_service()


def runners(service: KeibaService, race_id: str) -> list[int]:
    return sorted(service.data.get_race(race_id).runner_numbers())
