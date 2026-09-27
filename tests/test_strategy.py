from krflow.config import StrategyParams
from krflow.data.models import Flow, Bar
from krflow.strategy.signal import compute_candidates
from helpers import synth_db


def _set_flows(store, code, dates, inst, foreign, available=None):
    store.upsert_flows([Flow(code, d, i, f, 0, "KRW", True, "t", d, "x", available or f"{d} 18:00:00") for d, i, f in zip(dates, inst, foreign)])


def test_signal_requires_streak_and_foreign_on_last_day():
    con, store = synth_db(codes=3)
    days = store.dates()
    sig = days[10]
    win = days[8:11]
    _set_flows(store, "900000", win, [1e8, 2e8, 3e8], [-1, -1, 5e7])    # 통과
    _set_flows(store, "900001", win, [1e8, -2e8, 3e8], [1, 1, 5e7])     # 연속 아님 -> 후보 아님
    _set_flows(store, "900002", win, [1e8, 2e8, 3e8], [1, 1, -5e7])     # 외국인 순매도 -> 제외 사유
    p = StrategyParams(min_trading_value=0)
    c = {x.code: x for x in compute_candidates(store, sig, p, f"{days[11]} 08:30:00")}
    assert c["900000"].selected and c["900000"].rank == 1
    assert "900001" not in c
    assert not c["900002"].selected and "외국인" in c["900002"].exclusion
    # 기관 단독 전략이면 900002도 선정
    p2 = StrategyParams(min_trading_value=0, require_foreign=False)
    c2 = {x.code: x for x in compute_candidates(store, sig, p2, f"{days[11]} 08:30:00")}
    assert c2["900002"].selected


def test_point_in_time_excludes_late_data():
    con, store = synth_db(codes=2)
    days = store.dates()
    sig = days[10]
    win = days[8:11]
    _set_flows(store, "900000", win, [1e8, 2e8, 3e8], [1, 1, 5e7], available=f"{days[12]} 18:00:00")  # 이틀 뒤 공개
    p = StrategyParams(min_trading_value=0)
    codes = {x.code for x in compute_candidates(store, sig, p, f"{days[11]} 08:30:00")}
    assert "900000" not in codes
    codes_later = {x.code for x in compute_candidates(store, sig, p, f"{days[13]} 08:30:00")}
    assert "900000" in codes_later


def test_provisional_and_halted_are_excluded_with_reason():
    con, store = synth_db(codes=2)
    days = store.dates()
    sig = days[10]
    win = days[8:11]
    _set_flows(store, "900000", win, [1e8, 2e8, 3e8], [1, 1, 5e7])
    store.upsert_flows([Flow("900000", sig, 3e8, 5e7, 0, "KRW", False, "t", sig, "x", f"{sig} 18:00:00")])
    p = StrategyParams(min_trading_value=0)
    c = {x.code: x for x in compute_candidates(store, sig, p, f"{days[11]} 08:30:00")}
    assert not c["900000"].selected and "잠정" in c["900000"].exclusion
