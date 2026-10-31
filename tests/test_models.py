from datetime import date

import pytest

from models import BetType, canonical_race_id, combination_key, parse_bet_type, parse_race_id, week_start


def test_parse_race_id():
    k = parse_race_id("20261101-TOKYO-11")
    assert (k.date, k.course, k.number) == (date(2026, 11, 1), "TOKYO", 11)


def test_canonical_race_id_normalizes_case_and_padding():
    assert canonical_race_id("20261101-tokyo-1") == "20261101-TOKYO-01"


@pytest.mark.parametrize("bad", ["", "2026-11-01-TOKYO-11", "20261301-TOKYO-11", "20261101-TOKYO-13", "20261101-TOKYO-0", "x"])
def test_parse_race_id_rejects_bad(bad):
    with pytest.raises(ValueError):
        parse_race_id(bad)


@pytest.mark.parametrize(
    "value,expected",
    [("win", BetType.WIN), ("WIN", BetType.WIN), ("単勝", BetType.WIN), ("ワイド", BetType.WIDE),
     ("三連単", BetType.TRIFECTA), ("3連複", BetType.TRIO)],
)
def test_parse_bet_type(value, expected):
    assert parse_bet_type(value) is expected


def test_parse_bet_type_rejects_unknown():
    with pytest.raises(ValueError):
        parse_bet_type("枠連")


def test_combination_key():
    assert combination_key(BetType.QUINELLA, [5, 2]) == "2-5"
    assert combination_key(BetType.TRIO, [9, 1, 4]) == "1-4-9"
    assert combination_key(BetType.EXACTA, [5, 2]) == "5>2"
    assert combination_key(BetType.TRIFECTA, [9, 1, 4]) == "9>1>4"
    assert combination_key(BetType.WIN, [3]) == "3"


def test_week_is_tuesday_to_monday():
    tue = date(2026, 11, 3)
    for d in range(3, 10):  # 11/3(火)〜11/9(月)
        assert week_start(date(2026, 11, d)) == tue
    assert week_start(date(2026, 11, 10)) == date(2026, 11, 10)  # 次の火曜は新しい週
