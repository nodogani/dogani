"""
일정 파일(예: "2026일정.md") 파싱. todo 폴더 안 어디에 있어도 찾는다.

한 줄 = 일정 하나:  일시, 제목, #태그, 장소, 메모
  **261119** 15:00, 과제 중간실사, #과제, 대회의실, 메모할내용
  261116-261120,고객사 교육 준비, #교육
  **261125** 9:00-12:00, 과제 발표평가, #과제, 외부
  ==261127==,홍길동 연차, #연차

- 날짜: YYMMDD / YYYY-MM-DD, 기간은 시작-끝 또는 시작~끝 (끝은 YYMMDD, MMDD, DD 도 가능)
- 시간: 날짜 뒤에 "15:00" 또는 "9:00-12:00" (없어도 됨)
- 날짜를 **굵게** = 중요 표시, ==하이라이트== = 쉬는 날 (노트 화살표 이동에서 건너뜀),
  *기울임* = 기간을 화살표(←──→)로 표시
- "10:00-10:30?" 처럼 시간 끝에 ? = 그 사이 언젠가 (정확한 시각 모름)
- "-261119" = 시작 미정, 마감만 / "261119-" = 시작만, 끝 미정 (달력에서 페이드로 표시)
- 태그는 #으로 시작하는 칸(여러 개 가능), 태그가 아닌 칸은 순서대로 장소, 메모 (장소 없이 메모만: "제목, , 메모")
"""
import datetime as dt
import re

SCHEDULE_FILE_RE = re.compile(r"^(?:\d{4}\s*)?일정\.md$", re.I)

_DATE = r"(\d{6}|\d{4}-\d{2}-\d{2})"
_FIRST_RE = re.compile(_DATE + r"(?:\s*[-~]\s*(\d{4}-\d{2}-\d{2}|\d{6}|\d{4}|\d{2})(?!\d))?\s*(.*)$")
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}(?:\s*[-~]\s*\d{1,2}:\d{2})?\??$")
_OPEN_START_RE = re.compile(r"^[-~]\s*" + _DATE + r"(?!\d)\s*(.*)$")       # -261119 : 시작 미정, 마감만
_OPEN_END_RE = re.compile(r"^" + _DATE + r"\s*[-~](?=\s*$|\s+\d{1,2}:|\s*[^\d\s])\s*(.*)$")       # 261119- : 시작만, 끝 미정
_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[.\]\s+)?")
_MARK_RE = re.compile(r"\*|==")  # 굵게(**)·기울임(*)·하이라이트(==) 기호


def is_schedule_file(name):
    return bool(SCHEDULE_FILE_RE.match(name))


def _date(text):
    if "-" in text:
        return dt.date.fromisoformat(text)
    return dt.date(2000 + int(text[:2]), int(text[2:4]), int(text[4:6]))


def _end_date(text, start):
    if len(text) == 2:      # DD
        d = start.replace(day=int(text))
    elif len(text) == 4:    # MMDD
        d = start.replace(month=int(text[:2]), day=int(text[2:]))
    else:
        d = _date(text)
    if d < start and len(text) <= 4:  # 연/월 넘어감 (예: 261230-02)
        d = d.replace(year=d.year + 1) if len(text) == 4 else _add_month(d)
    return d


def _add_month(d):
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return d.replace(year=y, month=m)


def parse_line(line):
    """한 줄 → 일정 dict. 일정 줄이 아니면 None."""
    raw = line.strip()  # 달력에서 수정할 때 파일에서 이 줄을 찾는 데 씀
    line = _LIST_RE.sub("", line.strip(), count=1)
    if not line:  # 제목·인용 등 날짜로 시작하지 않는 줄은 아래 정규식에서 걸러짐
        return None
    fields = [f.strip() for f in re.split(r"[,，]", line)]
    head = fields[0]
    holiday = "==" in head
    important = "**" in head
    italic = bool(re.search(r"(?<!\*)\*(?!\*)", head.replace("***", "*")))  # *기울임* (***굵게+기울임*** 포함)
    plain = _MARK_RE.sub("", head).strip()
    open_ = ""
    try:
        if m := _OPEN_START_RE.match(plain):
            # 달력엔 마감 전날부터 그려서 앞쪽이 페이드로 들어오게
            open_, end = "start", _date(m[1])
            start, time = end - dt.timedelta(days=1), m[2]
        elif m := _OPEN_END_RE.match(plain):
            open_, start = "end", _date(m[1])
            end, time = start + dt.timedelta(days=1), m[2]
        elif m := _FIRST_RE.match(plain):
            start = _date(m[1])
            end = _end_date(m[2], start) if m[2] else start
            time = m[3]
        else:
            return None
    except ValueError:
        return None
    if end < start:
        start, end = end, start
    time = time.strip()
    if time and not _TIME_RE.match(time):
        # 날짜 뒤가 시간이 아니면 제목으로 본다 ("261119 과제 실사, ...")
        fields.insert(1, time)
        time = ""
    rest = [_MARK_RE.sub("", f).strip() for f in fields[1:]]
    title = rest[0] if rest else ""
    tags, others = [], []
    for f in rest[1:]:
        if f.startswith("#"):
            tags += [t.lstrip("#") for t in f.split() if t.startswith("#") and len(t) > 1]
        else:
            others.append(f)  # 빈 칸도 자리로 셈: "제목, , 메모" = 장소 없이 메모
    while others and not others[-1]:
        others.pop()
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "time": re.sub(r"\s*[-~]\s*", "-", time).rstrip("?"),
        "uncertain": time.endswith("?"),  # 10:00-10:30? = 그 사이 언젠가
        "open": open_,  # "start" = 시작 미정(-261119), "end" = 끝 미정(261119-)
        "title": title,
        "tags": tags,
        "place": others[0] if others else "",
        "memo": ", ".join(f for f in others[1:] if f),
        "holiday": holiday,
        "important": important,
        "italic": italic,
        "raw": raw,
    }


def sort_key(ev):
    """날짜순, 같은 날은 시간 없는 것(기간·종일) 먼저, 그다음 시간순 (9:30 < 15:00)"""
    t = ev["time"].split("-")[0]
    return ev["start"], t.zfill(5) if t else ""


def parse(text):
    events = []
    in_fence = False
    for line in text.splitlines():
        if re.match(r"^\s*(```|~~~)", line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        ev = parse_line(line)
        if ev:
            events.append(ev)
    events.sort(key=sort_key)
    return events
