"""거래일 달력. 주말 + 휴장일 파일. 특별 거래시간(단축/연장)도 기록한다."""
from __future__ import annotations

import csv
from datetime import date, timedelta
from pathlib import Path


# 고정 휴장일(요일 무관). 대체공휴일/추석/설은 연도별 파일로 관리한다.
FIXED_HOLIDAYS_MMDD = {"01-01", "03-01", "05-05", "06-06", "08-15", "10-03", "10-09", "12-25", "12-31"}


class TradingCalendar:
    def __init__(self, holidays: set[date] | None = None, special: dict[date, str] | None = None):
        self.holidays: set[date] = set(holidays or set())
        self.special: dict[date, str] = dict(special or {})  # date -> 설명 (예: "수능 1시간 지연")

    @classmethod
    def load(cls, path: str | Path | None) -> "TradingCalendar":
        """CSV 컬럼: date, kind(holiday|special), note"""
        cal = cls()
        if path and Path(path).exists():
            with open(path, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    d = date.fromisoformat(row["date"].strip())
                    if row.get("kind", "holiday").strip() == "special":
                        cal.special[d] = row.get("note", "")
                    else:
                        cal.holidays.add(d)
        return cal

    def is_trading_day(self, d: date) -> bool:
        if d.weekday() >= 5:
            return False
        if d.strftime("%m-%d") in FIXED_HOLIDAYS_MMDD:
            return False
        return d not in self.holidays

    def is_special_session(self, d: date) -> bool:
        return d in self.special

    def next_trading_day(self, d: date, n: int = 1) -> date:
        cur = d
        while n > 0:
            cur += timedelta(days=1)
            if self.is_trading_day(cur):
                n -= 1
        return cur

    def prev_trading_day(self, d: date, n: int = 1) -> date:
        cur = d
        while n > 0:
            cur -= timedelta(days=1)
            if self.is_trading_day(cur):
                n -= 1
        return cur

    def trading_days(self, start: date, end: date) -> list[date]:
        out = []
        cur = start
        while cur <= end:
            if self.is_trading_day(cur):
                out.append(cur)
            cur += timedelta(days=1)
        return out

    def nth_trading_day_from(self, start: date, n: int) -> date:
        """start를 1번째로 세어 n번째 거래일. n=5 -> 4 거래일 뒤."""
        if not self.is_trading_day(start):
            raise ValueError(f"{start}는 거래일이 아닙니다")
        return self.next_trading_day(start, n - 1) if n > 1 else start
