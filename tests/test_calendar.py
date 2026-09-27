from datetime import date

from krflow.calendar import TradingCalendar


def test_weekend_and_fixed_holidays():
    cal = TradingCalendar()
    assert not cal.is_trading_day(date(2025, 1, 1))     # 신정
    assert not cal.is_trading_day(date(2025, 1, 4))     # 토
    assert cal.is_trading_day(date(2025, 1, 2))


def test_nth_trading_day_counts_start_as_first():
    cal = TradingCalendar(holidays={date(2025, 1, 8)})
    # 1/6(월) 매수 -> 5번째 거래일: 1/6,1/7,1/9,1/10,1/13 (1/8 휴장)
    assert cal.nth_trading_day_from(date(2025, 1, 6), 5) == date(2025, 1, 13)
    assert cal.prev_trading_day(date(2025, 1, 9)) == date(2025, 1, 7)
