"""netkeiba のページ（HTML）の解析と race_id の変換。通信は行わない。

クラス名は下の定数にまとめてある。本物のページ（2026-11 に保存した調教 type=1・厩舎コメント）に合わせてある。
ページが変わると NetkeibaParseError になり、データは保存されないので、
`admin.py netkeiba-fetch --save-pages` で保存したページを見て、ここと tests/fixtures/netkeiba/ を直す。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag

from data.jra_parse import COURSE_BY_ALPHA
from models import parse_race_id

# 調教ページ（type=1「中間の調教全て」）。1頭の先頭の行が馬番・馬名で、続く行が1本ずつの追い切り（新しい順）
TRAINING_ROW = "table.OikiriTable tr.HorseList"
TRAINING_ALL_WORKOUTS = "table.OikiriTable.Oikiri_Between"  # type=2「最終追切のみ」の表にはこのクラスがない
TRAINING_NUMBER = "td.Umaban"
TRAINING_DAY = "td.Training_Day"  # クラスのないコースのセルは、このセルの次にある
TRAINING_LAP = "ul.TrainingTimeDataList li"
TRAINING_LOAD = "td.TrainingLoad"
TRAINING_CRITIC = "td.Training_Critic"
TRAINING_RANK = "td[class*=Rank_]"
# 基準より速い時計の印。基準はコースごとなので、コースをまたいで比べられる
# ページの説明はオレンジと黄色だけだが、保存したページではどのコース・区間でも 01 の方が速い
TRAINING_MUCH_FASTER = "li.TokeiColor01"
TRAINING_FASTER = "li.TokeiColor02"
# 厩舎コメントページ
COMMENT_ROW = "table.Stable_Comment tr"
COMMENT_NUMBER = "td.Waku"  # 枠は td.Waku1 のように数字つきのクラスで、別のセル
COMMENT_TEXT = "dl.Comment_Cell"
# 共通
ROW_NAME = ".Horse_Name"

# 中間調教の要点として残す本数
INTERIM_HIGHLIGHTS = 3


class NetkeibaParseError(Exception):
    """ページの構造が想定と違う、または載っているはずのデータがない。"""


@dataclass(frozen=True)
class LoginForm:
    action: str
    hidden: dict[str, str]
    user_field: str
    password_field: str


@dataclass(frozen=True)
class ParsedInterim:
    date: str
    time: str
    note: str


@dataclass(frozen=True)
class ParsedTraining:
    number: int
    name: str
    grade: str
    note: str
    final_time: str
    # 最終追い切りを含む本数。全本数の載ったページでなければ None（「不明」で、0本ではない）
    interim_count: int | None = None
    interim_highlights: tuple[ParsedInterim, ...] = ()


@dataclass(frozen=True)
class ParsedComment:
    number: int
    name: str
    comment: str


_CARD_CNAME_RE = re.compile(r"pw01dde\d{2}(\d{2})(\d{4})(\d{2})(\d{2})(\d{2})(\d{8})/[0-9A-F]{2}")


def netkeiba_race_id(card_cname: str, race_id: str) -> str:
    """JRA の出馬表の cname から netkeiba の race_id（年・場・回・日・R）を作る。

    pw01dde 01 05 2026 04 11 11 20261101/A6 -> 202605041111。race_id と食い違えば ValueError。
    """
    m = _CARD_CNAME_RE.fullmatch(card_cname)
    if m is None:
        raise ValueError(f"出馬表の cname の形式が想定と違います（{card_cname}）")
    course, year, kai, day, number, ymd = m.groups()
    k = parse_race_id(race_id)
    if (
        COURSE_BY_ALPHA.get(k.course, ("",))[0] != course
        or int(number) != k.number
        or ymd != f"{k.date:%Y%m%d}"
        or year != f"{k.date:%Y}"
    ):
        raise ValueError(f"出馬表の cname が {race_id} と一致しません（{card_cname}）")
    return f"{year}{course}{kai}{day}{number}"


def normalize_name(name: str) -> str:
    """馬名の照合用。全角半角の違いと空白を無視する。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", name))


def _text(node: Tag | None) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip() if node is not None else ""


def has_password_field(html: str) -> bool:
    """ログイン画面が返ってきたか（ログインに失敗した・ログインが切れた）。"""
    return BeautifulSoup(html, "html.parser").select_one("input[type=password]") is not None


def parse_login_form(html: str) -> LoginForm:
    """ログイン画面のフォームを読む。項目名は決め打ちしない。"""
    soup = BeautifulSoup(html, "html.parser")
    for form in soup.find_all("form"):
        password = form.select_one("input[type=password]")
        user = form.select_one("input[type=text], input[type=email], input:not([type])")
        if password is None or user is None or not password.get("name") or not user.get("name"):
            continue
        hidden = {i["name"]: i.get("value", "") for i in form.select("input[type=hidden]") if i.get("name")}
        return LoginForm(str(form.get("action") or ""), hidden, str(user["name"]), str(password["name"]))
    raise NetkeibaParseError("ログインフォームが見つかりません")


def _row_key(tr: Tag, number_selector: str) -> tuple[int, str] | None:
    """馬番と馬名。枠順確定前で馬番がない行は None。"""
    number = _text(tr.select_one(number_selector))
    name = _text(tr.select_one(ROW_NAME))
    return (int(number), name) if number.isdecimal() and name else None


def _horse_blocks(rows: list[Tag]) -> list[list[Tag]]:
    """調教の表の行を1頭分ずつに分ける。馬番のセルがある行が先頭。"""
    blocks: list[list[Tag]] = []
    for tr in rows:
        if tr.select_one(TRAINING_NUMBER) is not None:
            blocks.append([tr])
        elif blocks:
            blocks[-1].append(tr)
    return blocks


def _workout_time(tr: Tag) -> str:
    """コース・時計・脚色（例: 美Ｗ 84.0-68.0-53.0-38.0-12.0 馬也）。括弧のラップは入れない。"""
    day = tr.select_one(TRAINING_DAY)
    course = _text(day.find_next_sibling("td")) if day is not None else ""
    laps = ["".join(li.find_all(string=True, recursive=False)).strip() for li in tr.select(TRAINING_LAP)]
    times = "-".join(lap for lap in laps if lap not in ("", "-"))  # 計測のない区間は "-"
    return " ".join(filter(None, [course, times, _text(tr.select_one(TRAINING_LOAD))]))


def _strength(tr: Tag) -> tuple[int, str]:
    """強い順に並べるキー。基準より速い時計の印が多いほど強く、同点なら評価（A〜D）が高い方。"""
    marks = 2 * len(tr.select(TRAINING_MUCH_FASTER)) + len(tr.select(TRAINING_FASTER))
    return -marks, _text(tr.select_one(TRAINING_RANK)) or "Z"


def _highlights(interims: list[Tag]) -> tuple[ParsedInterim, ...]:
    """最終追い切りを除いた中で強い順に選ぶ。同点なら新しい方（sorted は順番を保つ）。"""
    return tuple(
        ParsedInterim(_text(tr.select_one(TRAINING_DAY)), _workout_time(tr), _text(tr.select_one(TRAINING_CRITIC)))
        for tr in sorted(interims, key=_strength)[:INTERIM_HIGHLIGHTS]
    )


def parse_training(html: str) -> list[ParsedTraining]:
    """調教ページ。評価・短評・時計がどれも載っていなければ NetkeibaParseError。"""
    soup = BeautifulSoup(html, "html.parser")
    all_workouts = soup.select_one(TRAINING_ALL_WORKOUTS) is not None
    out: list[ParsedTraining] = []
    for block in _horse_blocks(soup.select(TRAINING_ROW)):
        key = _row_key(block[0], TRAINING_NUMBER)
        if key is None:
            continue
        workouts = [tr for tr in block if tr.select_one(TRAINING_DAY) is not None]
        final = workouts[0] if workouts else block[0]
        out.append(
            ParsedTraining(
                number=key[0],
                name=key[1],
                grade=_text(final.select_one(TRAINING_RANK)),
                note=_text(final.select_one(TRAINING_CRITIC)),
                final_time=_workout_time(final),
                interim_count=len(workouts) if all_workouts and workouts else None,
                interim_highlights=_highlights(workouts[1:]) if all_workouts else (),
            )
        )
    if not any(t.grade or t.note or t.final_time for t in out):
        raise NetkeibaParseError("調教ページからデータを読み取れません（未公開・権限不足・ページ構造の変更）")
    return out


def parse_comments(html: str) -> list[ParsedComment]:
    """厩舎コメントページ。コメントが1件も載っていなければ NetkeibaParseError。"""
    out: list[ParsedComment] = []
    for tr in BeautifulSoup(html, "html.parser").select(COMMENT_ROW):
        key = _row_key(tr, COMMENT_NUMBER)
        comment = _text(tr.select_one(COMMENT_TEXT))
        if key is not None and comment:
            out.append(ParsedComment(key[0], key[1], comment))
    if not out:
        raise NetkeibaParseError("厩舎コメントのページからデータを読み取れません（未公開・権限不足・ページ構造の変更）")
    return out
