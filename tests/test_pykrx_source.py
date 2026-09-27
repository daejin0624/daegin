"""pykrx 1.2.x의 실제 반환 형식(docstring 예시)을 흉내 낸 가짜 API로 수집기를 검증한다."""
from datetime import date, datetime

import pytest

pd = pytest.importorskip("pandas")

from krflow.calendar import TradingCalendar
from krflow.db import connect
from krflow.data.store import MarketStore
from krflow.data.quality import run_quality_checks
from krflow.data import pykrx_source


class FakeAPI:
    def __init__(self, empty_dates=(), fail_dates=(), delisted_after=None):
        self.empty, self.fail, self.delisted_after = set(empty_dates), set(fail_dates), delisted_after

    def _codes(self, ymd, mkt):
        codes = ["005930", "000660"] if mkt == "KOSPI" else ["035720"]
        if mkt == "KOSDAQ" and not (self.delisted_after and ymd > self.delisted_after):
            codes.append("999990")    # 나중에 상장폐지되는 종목
        return codes

    def get_market_ohlcv_by_ticker(self, ymd, mkt):
        if ymd in self.fail:
            raise ConnectionError("KRX 응답 없음")
        cols = ["시가", "고가", "저가", "종가", "거래량", "거래대금", "등락률"]
        if ymd in self.empty:
            return pd.DataFrame(columns=cols)
        rows = {}
        for c in self._codes(ymd, mkt):
            if c == "000660" and ymd == "20250106":
                rows[c] = [0, 0, 0, 20000, 0, 0, 0.0]          # 거래정지: 종가만
            else:
                rows[c] = [10000, 10500, 9900, 10200, 100000, 2_000_000_000, 1.0]
        df = pd.DataFrame.from_dict(rows, orient="index", columns=cols)
        df.index.name = "티커"
        return df

    def get_market_net_purchases_of_equities_by_ticker(self, f, t, mkt, investor):
        cols = ["종목명", "매도거래량", "매수거래량", "순매수거래량", "매도거래대금", "매수거래대금", "순매수거래대금"]
        v = {"기관합계": 3e8, "외국인": 1e8, "기타외국인": 1e7}[investor]
        rows = {c: [f"name{c}", 0, 0, 0, 0, 0, v] for c in self._codes(f, mkt)}
        df = pd.DataFrame.from_dict(rows, orient="index", columns=cols)
        df.index.name = "티커"
        return df


def test_fetch_day_parses_bars_flows_and_halts():
    r, uni = pykrx_source.fetch_day(date(2025, 1, 6), FakeAPI(), now=datetime(2025, 1, 7, 9, 0))
    assert r.status == "ok" and r.received == 2
    bars = {b.code: b for b in r.bars}
    assert bars["000660"].halted and not bars["000660"].open_tradable and bars["000660"].open is None
    f = {x.code: x for x in r.flows}["005930"]
    assert f.inst_net == 3e8 and f.foreign_net == 1e8 + 1e7 and f.unit == "KRW" and f.is_final
    assert ("035720", "name035720", "KOSDAQ", None, None) in uni


def test_same_day_collection_before_18_is_provisional():
    r, _ = pykrx_source.fetch_day(date(2025, 1, 6), FakeAPI(), now=datetime(2025, 1, 6, 16, 0))
    assert all(not f.is_final for f in r.flows) and "잠정" in r.message


def test_empty_or_failed_is_not_reported_ok():
    r, _ = pykrx_source.fetch_day(date(2025, 1, 6), FakeAPI(empty_dates={"20250106"}))
    assert r.status == "failed" and "빈 결과" in r.message
    r, _ = pykrx_source.fetch_day(date(2025, 1, 6), FakeAPI(fail_dates={"20250106"}))
    assert r.status == "failed" and "ConnectionError" in r.message


def test_collect_range_resumes_and_keeps_delisted_codes():
    con = connect(":memory:")
    store = MarketStore(con)
    api = FakeAPI(delisted_after="20250107")
    s = pykrx_source.collect_range(store, date(2025, 1, 2), date(2025, 1, 10), TradingCalendar(), api, pause=0, log=lambda *_: None)
    assert s["ok"] == 7    # 2025-01-02 ~ 01-10 거래일 7일
    s2 = pykrx_source.collect_range(store, date(2025, 1, 2), date(2025, 1, 10), TradingCalendar(), api, pause=0, log=lambda *_: None)
    assert s2["skipped"] == s["ok"] and s2["ok"] == 0
    u = con.execute("SELECT listed_date, delisted_date FROM universe WHERE code='999990'").fetchone()
    assert u["delisted_date"] == "2025-01-07"
    # 상장폐지 이후 날짜를 '시세 누락'으로 잡지 않는다
    new = run_quality_checks(con)
    assert not [i for i in new if i["kind"] == "missing_bar"]
    logs = con.execute("SELECT COUNT(*) FROM collection_log WHERE source='pykrx'").fetchone()[0]
    assert logs == s["ok"]


def test_partial_day_is_refetched_on_resume():
    con = connect(":memory:")
    store = MarketStore(con)

    class HalfBroken(FakeAPI):
        broken = True
        def get_market_net_purchases_of_equities_by_ticker(self, f, t, mkt, investor):
            if self.broken and mkt == "KOSDAQ":
                raise ConnectionError("x")
            return super().get_market_net_purchases_of_equities_by_ticker(f, t, mkt, investor)

    api = HalfBroken()
    s1 = pykrx_source.collect_range(store, date(2025, 1, 2), date(2025, 1, 2), TradingCalendar(), api, pause=0, log=lambda *_: None)
    assert s1["partial"] == 1
    api.broken = False
    s2 = pykrx_source.collect_range(store, date(2025, 1, 2), date(2025, 1, 2), TradingCalendar(), api, pause=0, log=lambda *_: None)
    assert s2["ok"] == 1 and s2["skipped"] == 0
    assert con.execute("SELECT COUNT(*) FROM daily_bars WHERE code='035720'").fetchone()[0] == 1
