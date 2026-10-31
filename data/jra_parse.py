"""JRA 公式サイト（www.jra.go.jp/JRADB/）の HTML を解析する純粋関数。

通信は行わない（scraper.py が取得した HTML を渡す）。ページ構造が変わったらここだけ直す。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time

from bs4 import BeautifulSoup, Tag

from models import JST, BetType, Horse, PastRace, combination_key

# JRA の場コード -> (race_id に使う英字コード, 日本語名)
COURSE_CODES: dict[str, tuple[str, str]] = {
    "01": ("SAPPORO", "札幌"),
    "02": ("HAKODATE", "函館"),
    "03": ("FUKUSHIMA", "福島"),
    "04": ("NIIGATA", "新潟"),
    "05": ("TOKYO", "東京"),
    "06": ("NAKAYAMA", "中山"),
    "07": ("CHUKYO", "中京"),
    "08": ("KYOTO", "京都"),
    "09": ("HANSHIN", "阪神"),
    "10": ("KOKURA", "小倉"),
}
COURSE_BY_ALPHA = {alpha: (code, name) for code, (alpha, name) in COURSE_CODES.items()}
COURSE_BY_NAME = {name: alpha for alpha, name in COURSE_CODES.values()}

# オッズページの種別番号（cname の pw15X）
ODDS_KIND: dict[BetType, str] = {
    BetType.WIN: "1",
    BetType.PLACE: "1",
    BetType.QUINELLA: "4",
    BetType.WIDE: "5",
    BetType.EXACTA: "6",
    BetType.TRIO: "7",
    BetType.TRIFECTA: "8",
}

# 払戻金の li クラス -> 券種（枠連 wakuren は扱わない）
PAYOUT_CLASS: dict[str, BetType] = {
    "win": BetType.WIN,
    "place": BetType.PLACE,
    "umaren": BetType.QUINELLA,
    "wide": BetType.WIDE,
    "umatan": BetType.EXACTA,
    "trio": BetType.TRIO,
    "tierce": BetType.TRIFECTA,
}

REFUND_MARKS = ("取消", "除外")  # 返還対象
SCRATCH_MARKS = ("取消", "除外")


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _text(node: Tag | None) -> str:
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _int(s: str | None) -> int | None:
    if s is None:
        return None
    m = re.search(r"-?\d+", s.replace(",", ""))
    return int(m.group(0)) if m else None


def _float(s: str | None) -> float | None:
    if s is None:
        return None
    m = re.search(r"\d+(?:\.\d+)?", s.replace(",", ""))
    return float(m.group(0)) if m else None


# --------------------------------------------------------------------------
# 開催・レース一覧（cname の抽出）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MeetingLink:
    course: str  # 英字コード（TOKYO 等）
    date: date
    path: str
    cname: str


_MEETING_RE = re.compile(
    r"doAction\('(/JRADB/access[DS]\.html)',\s*'(pw01[ds]rl[01]0(\d{2})\d{4}\d{2}\d{2}(\d{8})/[0-9A-F]{2})'\)"
)


def parse_meeting_links(html: str) -> list[MeetingLink]:
    """出馬表・レース結果の開催選択ページから、開催（日付×場）ごとのレース一覧リンクを取り出す。"""
    out: dict[tuple[str, date], MeetingLink] = {}
    for path, cname, code, ymd in _MEETING_RE.findall(html):
        if code not in COURSE_CODES:
            continue
        d = datetime.strptime(ymd, "%Y%m%d").date()
        key = (COURSE_CODES[code][0], d)
        out.setdefault(key, MeetingLink(course=key[0], date=d, path=path, cname=cname))
    return list(out.values())


_RACE_CNAME_RE = {
    "card": re.compile(r"pw01dde\d{2}(\d{2})\d{4}\d{2}\d{2}(\d{2})(\d{8})/[0-9A-F]{2}"),
    "result": re.compile(r"pw01sde\d{2}(\d{2})\d{4}\d{2}\d{2}(\d{2})(\d{8})/[0-9A-F]{2}"),
    "odds": re.compile(r"pw151ouS3(\d{2})\d{4}\d{2}\d{2}(\d{2})(\d{8})Z/[0-9A-F]{2}"),
}


def parse_race_links(html: str) -> dict[int, dict[str, str]]:
    """レース一覧ページから {レース番号: {"card"|"result"|"odds": cname}} を取り出す。

    結果リンクはレース確定後にだけ現れる。
    """
    out: dict[int, dict[str, str]] = {}
    for kind, rx in _RACE_CNAME_RE.items():
        for m in rx.finditer(html):
            out.setdefault(int(m.group(2)), {}).setdefault(kind, m.group(0))
    return out


_ODDS_LINK_RE = re.compile(r"pw15([3-8])ouS3(\d{2})\d{4}\d{2}\d{2}(\d{2})(\d{8})Z(\d{2})?/[0-9A-F]{2}")


def parse_odds_links(html: str, race_number: int) -> dict[str, str]:
    """単勝・複勝オッズページから、同じレースの他券種オッズページの cname を取り出す。{種別番号: cname}"""
    out: dict[str, str] = {}
    for m in _ODDS_LINK_RE.finditer(html):
        kind, _code, rnum, _ymd, suffix = m.groups()
        if int(rnum) != race_number:
            continue
        if suffix not in (None, "99"):  # 3連複の軸馬指定ページは使わない（99 = すべて）
            continue
        out.setdefault(kind, m.group(0))
    return out


# --------------------------------------------------------------------------
# 出馬表
# --------------------------------------------------------------------------


@dataclass
class ParsedRace:
    number: int
    date: date
    course_name: str
    post_time: datetime
    name: str
    race_class: str
    surface: str
    distance: int
    horses: list[Horse] = field(default_factory=list)


_DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日.*?\d+回(\D+?)\d+日")
_TIME_RE = re.compile(r"(\d{1,2})時(\d{1,2})分")


def _parse_header(header: Tag) -> ParsedRace:
    m = _DATE_RE.search(_text(header.select_one(".date_line .date")))
    if not m:
        raise ValueError("出馬表の日付を解析できません")
    d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    t = _TIME_RE.search(_text(header.select_one(".date_line .time")))
    if not t:
        raise ValueError("出馬表の発走時刻を解析できません")
    post = datetime.combine(d, time(int(t.group(1)), int(t.group(2))), tzinfo=JST)
    num_img = header.select_one(".race_number img")
    number = _int(num_img.get("alt") if num_img else None)
    if number is None:
        raise ValueError("出馬表のレース番号を解析できません")
    course_cell = header.select_one(".type .course")
    detail = _text(course_cell.select_one(".detail")) if course_cell else ""
    surface = re.sub(r"[（）()]", "", detail).split("・")[0] if detail else ""
    distance = _int(_text(course_cell).replace(detail, "")) if course_cell else None
    race_class = " ".join(
        _text(header.select_one(f".type .{c}")) for c in ("category", "class", "rule", "weight")
    ).strip()
    race_class = re.sub(r"\s+", " ", race_class)
    return ParsedRace(
        number=number,
        date=d,
        course_name=m.group(4).strip(),
        post_time=post,
        name=_text(header.select_one(".race_name")),
        race_class=race_class,
        surface=surface,
        distance=distance or 0,
    )


def _parse_past(td: Tag) -> PastRace | None:
    if not td.select_one(".date_line .date"):
        return None
    num = td.select_one(".place_line .num")
    return PastRace(
        date=_text(td.select_one(".date_line .date")),
        course=_text(td.select_one(".date_line .rc")),
        race_name=_text(td.select_one(".race_line .name")),
        place=_text(td.select_one(".place_line .place")).replace(" ", ""),
        field_size=_int(_text(num.select_one(".max"))) if num else None,
        popularity=_int(_text(num.select_one(".pop"))) if num else None,
        jockey=_text(td.select_one(".info_line1 .jockey")),
        weight=_float(_text(td.select_one(".info_line1 .weight"))),
        distance=_text(td.select_one(".info_line2 .dist")),
        time=_text(td.select_one(".info_line2 .time")),
        condition=_text(td.select_one(".info_line2 .condition")),
        horse_weight=_int(_text(td.select_one(".info_line2 .h_weight"))),
        corners=tuple(n for n in (_int(_text(li)) for li in td.select(".corner_list li")) if n is not None),
        last_3f=_text(td.select_one(".info_line3 .f3")).replace("3F", "").strip(),
        winner_or_second=_text(td.select_one(".info_line3 .fin")),
    )


_HW_RE = re.compile(r"(\d+)\s*(?:kg)?\s*\(([+-]?\d+)\)")


def _parse_horse_row(tr: Tag, fallback_number: int) -> Horse:
    num_text = _text(tr.select_one("td.num"))
    scratched = any(mark in num_text for mark in SCRATCH_MARKS)
    number = _int(num_text) if not scratched else None
    if number is None:
        number = fallback_number  # 取消・除外は馬番の代わりに文字が表示される

    waku_img = tr.select_one("td.waku img")
    frame = _int(waku_img.get("alt")) if waku_img else None

    horse_td = tr.select_one("td.horse")
    detailed = horse_td is not None and horse_td.select_one(".name_line") is not None
    if detailed:
        name = _text(horse_td.select_one(".name_line .name"))
        odds_text = _text(horse_td.select_one(".odds_line .num"))
        pop = _int(_text(horse_td.select_one(".pop_rank")))
        hw_text = _text(horse_td.select_one(".result_line .weight"))
        jockey_td = tr.select_one("td.jockey")
        sex_age = _text(jockey_td.select_one("p.age")).split("/")[0] if jockey_td else ""
        weight = _float(_text(jockey_td.select_one("p.weight"))) if jockey_td else None
        jockey = _text(jockey_td.select_one("p.jockey")) if jockey_td else ""
        trainer = _text(horse_td.select_one("p.trainer a"))
        sire = _text(horse_td.select_one("li.sire")).replace("父：", "").strip()
        mare = horse_td.select_one("li.mare")
        dam_sire = ""
        dam = ""
        if mare:
            bm = mare.select_one(".bloodmare")
            dam_sire = re.sub(r"[（）()]|母の父：", "", _text(bm)).strip() if bm else ""
            if bm:
                bm.extract()
            dam = _text(mare).replace("母：", "").strip()
        past = tuple(p for p in (_parse_past(td) for td in tr.select("td.past")) if p is not None)
    else:
        name = _text(horse_td.select_one("a")) if horse_td else ""
        odds_text = _text(tr.select_one("td.odds"))
        pop = None
        hw_text = _text(tr.select_one("td.h_weight"))
        sex_age = _text(tr.select_one("td.age"))
        weight = _float(_text(tr.select_one("td.weight")))
        jockey = _text(tr.select_one("td.jockey"))
        trainer = _text(tr.select_one("td.trainer"))
        sire = dam = dam_sire = ""
        past = ()

    hw = _HW_RE.search(hw_text)
    win_odds = None if scratched or any(m in odds_text for m in SCRATCH_MARKS) else _float(odds_text)
    return Horse(
        number=number,
        name=name,
        jockey=jockey,
        scratched=scratched,
        frame=frame,
        sex_age=sex_age,
        weight=weight,
        trainer=trainer,
        horse_weight=int(hw.group(1)) if hw else None,
        horse_weight_diff=int(hw.group(2)) if hw else None,
        win_odds=win_odds,
        popularity=pop,
        sire=sire,
        dam=dam,
        dam_sire=dam_sire,
        past_races=past,
    )


def parse_race_cards(html: str) -> list[ParsedRace]:
    """出馬表ページ（1レースの詳細版・全レース一覧版のどちらも）を解析する。"""
    soup = _soup(html)
    races: list[ParsedRace] = []
    for header in soup.select("div.race_header"):
        table = header.find_parent("table")
        if table is None:
            continue
        race = _parse_header(header)
        tbody = table.find("tbody")
        rows = tbody.find_all("tr", recursive=False) if tbody else []
        prev = 0
        for tr in rows:
            if tr.select_one("td.num") is None:
                continue
            h = _parse_horse_row(tr, prev + 1)
            prev = h.number
            race.horses.append(h)
        races.append(race)
    return races


# --------------------------------------------------------------------------
# オッズ
# --------------------------------------------------------------------------


def _odds_value(td: Tag | None) -> float | None:
    """オッズのセル。複勝・ワイドの幅表示は下限値。空欄・取消は None。"""
    if td is None:
        return None
    low = td.select_one(".min")
    text = _text(low) if low else _text(td)
    if not re.fullmatch(r"\d+(?:,\d{3})*(?:\.\d+)?", text):
        return None
    return float(text.replace(",", ""))


def parse_win_place_odds(html: str) -> dict[BetType, dict[str, float]]:
    soup = _soup(html)
    win: dict[str, float] = {}
    place: dict[str, float] = {}
    for tr in soup.select("tr"):
        num = _int(_text(tr.select_one("td.num")))
        if num is None:
            continue
        w = _odds_value(tr.select_one("td.odds_tan"))
        p = _odds_value(tr.select_one("td.odds_fuku"))
        if w is not None:
            win[str(num)] = w
        if p is not None:
            place[str(num)] = p
    return {BetType.WIN: win, BetType.PLACE: place}


_PAIR_TABLE_CLASS = {BetType.QUINELLA: "umaren", BetType.WIDE: "wide", BetType.EXACTA: "umatan"}


def parse_pair_odds(html: str, bet_type: BetType) -> dict[str, float]:
    """馬連・ワイド・馬単。表ごとに caption が1頭目、行見出しが2頭目。"""
    soup = _soup(html)
    out: dict[str, float] = {}
    for table in soup.select(f"table.{_PAIR_TABLE_CLASS[bet_type]}"):
        first = _int(_text(table.find("caption")))
        if first is None:
            continue
        for tr in table.select("tbody tr"):
            second = _int(_text(tr.find("th")))
            v = _odds_value(tr.find("td"))
            if second is None or v is None or second == first:
                continue
            out[combination_key(bet_type, (first, second))] = v
    return out


def parse_trio_odds(html: str) -> dict[str, float]:
    """3連複。caption が「1頭目-2頭目」、行見出しが3頭目。"""
    soup = _soup(html)
    out: dict[str, float] = {}
    for table in soup.select("table.fuku3"):
        pair = re.findall(r"\d+", _text(table.find("caption")))
        if len(pair) != 2:
            continue
        a, b = int(pair[0]), int(pair[1])
        for tr in table.select("tbody tr"):
            c = _int(_text(tr.find("th")))
            v = _odds_value(tr.find("td"))
            if c is None or v is None:
                continue
            out[combination_key(BetType.TRIO, (a, b, c))] = v
    return out


def parse_trifecta_odds(html: str) -> dict[str, float]:
    """3連単。ブロックごとに1着・2着が見出し、行見出しが3着。"""
    soup = _soup(html)
    out: dict[str, float] = {}
    for li in soup.select("ul.tan3_list > li"):
        heads: list[int] = []
        for pl in li.select("div.p_line"):
            n = _int(_text(pl.select_one("div.num")))
            if n is not None:
                heads.append(n)
        if len(heads) < 2:
            continue
        a, b = heads[0], heads[1]
        for tr in li.select("table.tan3 tbody tr"):
            c = _int(_text(tr.find("th")))
            v = _odds_value(tr.find("td"))
            if c is None or v is None:
                continue
            out[combination_key(BetType.TRIFECTA, (a, b, c))] = v
    return out


def parse_odds(html: str, bet_type: BetType) -> dict[str, float]:
    if bet_type in (BetType.WIN, BetType.PLACE):
        return parse_win_place_odds(html)[bet_type]
    if bet_type in _PAIR_TABLE_CLASS:
        return parse_pair_odds(html, bet_type)
    if bet_type is BetType.TRIO:
        return parse_trio_odds(html)
    return parse_trifecta_odds(html)


# --------------------------------------------------------------------------
# レース結果
# --------------------------------------------------------------------------


@dataclass
class ParsedResult:
    order: list[int]
    refunded_horses: set[int]
    payouts: dict[str, dict[str, int]]


def parse_result(html: str) -> ParsedResult | None:
    """レース結果ページを解析する。払戻金が無ければ（未確定）None。"""
    soup = _soup(html)
    placed: list[tuple[int, int, int]] = []  # (着順, 表の順, 馬番)
    refunded: set[int] = set()
    for i, tr in enumerate(soup.select("tr")):
        place_td = tr.select_one("td.place")
        num = _int(_text(tr.select_one("td.num")))
        if place_td is None or num is None:
            continue
        place_text = _text(place_td)
        if any(m in place_text for m in REFUND_MARKS):
            refunded.add(num)
            continue
        p = _int(place_text)
        if p is not None:
            placed.append((p, i, num))

    payouts: dict[str, dict[str, int]] = {}
    for li in soup.select(".refund_area li"):
        classes = li.get("class") or []
        bt = next((PAYOUT_CLASS[c] for c in classes if c in PAYOUT_CLASS), None)
        if bt is None:
            continue
        for line in li.select("div.line"):
            nums = [int(n) for n in re.findall(r"\d+", _text(line.select_one("div.num")))]
            yen = _int(_text(line.select_one("div.yen")))
            if len(nums) != bt.size or yen is None:
                continue
            payouts.setdefault(bt.code, {})[combination_key(bt, nums)] = yen

    if not placed or not payouts:
        return None
    order = [num for _, _, num in sorted(placed)]
    return ParsedResult(order=order, refunded_horses=refunded, payouts=payouts)
