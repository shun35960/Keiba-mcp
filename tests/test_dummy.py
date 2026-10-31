from datetime import date, datetime, timedelta

from data.dummy import DummyDataSource
from models import JST, BetType, combination_key


def test_race_card_is_deterministic():
    a = DummyDataSource().get_races(date(2026, 11, 7))
    b = DummyDataSource().get_races(date(2026, 11, 7))
    assert a == b
    assert len(a) == 36  # 3場 × 12R
    assert {r.number for r in a} == set(range(1, 13))


def test_course_filter_accepts_code_and_japanese():
    ds = DummyDataSource()
    assert {r.course for r in ds.get_races(date(2026, 11, 7), "tokyo")} == {"TOKYO"}
    assert {r.course for r in ds.get_races(date(2026, 11, 7), "東京")} == {"TOKYO"}
    assert ds.get_races(date(2026, 11, 7), "NAKAYAMA") == []


def test_main_race_post_time():
    race = DummyDataSource().get_race("20261107-TOKYO-11")
    assert race.post_time == datetime(2026, 11, 7, 15, 50, tzinfo=JST)


def test_odds_cover_all_combinations():
    ds = DummyDataSource()
    race = ds.get_race("20261107-TOKYO-11")
    n = len(race.horses)
    assert len(ds.get_odds(race.race_id, BetType.WIN)) == n
    assert len(ds.get_odds(race.race_id, BetType.QUINELLA)) == n * (n - 1) // 2
    assert len(ds.get_odds(race.race_id, BetType.EXACTA)) == n * (n - 1)
    assert len(ds.get_odds(race.race_id, BetType.TRIFECTA)) == n * (n - 1) * (n - 2)
    assert all(o >= 1.0 for o in ds.get_odds(race.race_id, BetType.WIN).values())


def test_result_payouts_match_finish_order():
    res = DummyDataSource(instant_results=True).get_result("20261107-TOKYO-11")
    a, b, c = res.order[:3]
    assert res.payout_per_100(BetType.WIN, [a]) > 0
    assert res.payout_per_100(BetType.WIN, [b]) == 0
    assert res.payout_per_100(BetType.QUINELLA, [b, a]) > 0
    assert res.payout_per_100(BetType.EXACTA, [b, a]) == 0
    assert res.payout_per_100(BetType.TRIFECTA, [a, b, c]) > 0
    assert len(res.payouts["place"]) == 3 and len(res.payouts["wide"]) == 3
    assert all(v >= 100 and v % 10 == 0 for d in res.payouts.values() for v in d.values())
    assert combination_key(BetType.TRIO, [c, a, b]) in res.payouts["trio"]


def test_result_not_available_before_race_unless_instant():
    race_id = "20261107-TOKYO-11"
    now = datetime(2026, 11, 7, 9, 0, tzinfo=JST)
    ds = DummyDataSource(instant_results=False, clock=lambda: now)
    assert ds.get_result(race_id) is None
    later = DummyDataSource(instant_results=False, clock=lambda: now + timedelta(hours=8))
    assert later.get_result(race_id) is not None
