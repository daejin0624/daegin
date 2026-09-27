import copy
import dataclasses

from krflow.config import Settings
from krflow.backtest.engine import run_backtest
from krflow.backtest.metrics import summarize
from krflow.backtest.compare import compare_strategies, cost_sensitivity
from helpers import synth_db


def _settings(**risk):
    s = Settings(mode="synthetic")
    s.strategy.min_trading_value = 0
    for k, v in risk.items():
        setattr(s.risk, k, v)
    return s


def test_backtest_is_deterministic_and_respects_limits():
    con, store = synth_db(codes=30, seed=5)
    s = _settings(max_positions=3, budget_per_trade=500_000, max_daily_buy=1_000_000)
    r1 = run_backtest(store, s, "2024-02-01", "2024-12-31")
    r2 = run_backtest(store, s, "2024-02-01", "2024-12-31")
    assert [t.to_dict() for t in r1.trades] == [t.to_dict() for t in r2.trades]
    assert r1.data_fingerprint == r2.data_fingerprint
    m = summarize(r1)
    assert m["trades"] > 0
    # 보유 한도 / 일별 한도 / 중복 보유
    held = {}
    for e in r1.equity_curve:
        pass
    by_day = {}
    for t in r1.trades:
        by_day.setdefault(t.entry_date, []).append(t)
        assert t.entry_price * t.qty <= 500_000 * 1.001
    for d, ts in by_day.items():
        assert len(ts) <= 3
        assert sum(t.entry_price * t.qty for t in ts) <= 1_000_000
    # 동일 종목 보유 기간 겹침 없음
    for code in {t.code for t in r1.trades}:
        ivs = sorted((t.entry_date, t.exit_date or "9999") for t in r1.trades if t.code == code)
        for (a1, b1), (a2, b2) in zip(ivs, ivs[1:]):
            assert b1 < a2, f"{code} 중복 보유: {ivs}"
    # 청산 규칙: 매수일 포함 5번째 거래일
    days = store.dates()
    for t in r1.trades:
        if t.status == "closed" and not t.notes:
            assert days.index(t.exit_date) - days.index(t.entry_date) == 4


def test_engine_detects_injected_flow_effect():
    _, s0 = synth_db(codes=30, seed=9, flow_effect=0.0)
    _, s1 = synth_db(codes=30, seed=9, flow_effect=0.3)
    s = _settings()
    m0 = summarize(run_backtest(s0, s, "2024-02-01", "2024-12-31"))
    m1 = summarize(run_backtest(s1, s, "2024-02-01", "2024-12-31"))
    assert m1["avg_trade_return"] > 0.02 > m0["avg_trade_return"]
    assert m1["win_rate"] > 0.6


def test_exit_blocked_when_halted_then_retried():
    con, store = synth_db(codes=6, seed=2, inject_issues=True)   # 종목1이 40~42일째 거래정지
    from krflow.data.models import Flow
    days = store.dates()
    # 종목1이 36일째 신호 -> 37일 매수 -> 41일 청산 예정(정지) -> 43일 청산
    for d in days[28:34]:   # 주입한 신호 이전에는 자연 발생 신호가 없도록 기관 순매도
        store.upsert_flows([Flow("900001", d, -1e9, 1e9, 0, "KRW", True, "t", d, "x", f"{d} 18:00:00")])
    for d in days[34:37]:
        store.upsert_flows([Flow("900001", d, 1e9, 1e9, 0, "KRW", True, "t", d, "x", f"{d} 18:00:00")])
    s = _settings(max_positions=10)   # 다른 종목이 자리를 차지하지 않도록
    r = run_backtest(store, s, days[30], days[60])
    t = next(t for t in r.trades if t.code == "900001")
    assert t.entry_date == days[37] and t.planned_exit_date == days[41]
    assert t.exit_date == days[43] and any("매도 불가" in n for n in t.notes)


def test_costs_reduce_return_monotonically_per_trade():
    _, store = synth_db(codes=30, seed=11)
    s = _settings(max_loss_rate=10.0)  # 손실중단 없이 순수 비용 효과
    rows = cost_sensitivity(store, s, "2024-02-01", "2024-12-31")
    avg = [r["avg_trade_return"] for r in rows]
    assert avg[0] > avg[1] > avg[2] > avg[3] > avg[4] > avg[5]


def test_compare_variants_and_effects():
    _, store = synth_db(codes=30, seed=12)
    out = compare_strategies(store, _settings(), "2024-02-01", "2024-12-31")
    assert set(out["_effects"]) == {"foreign_condition_effect", "rank_inst_sum_effect", "rank_foreign_net_effect"}
    assert out["_benchmark"]["return"] is not None


def test_backtest_skips_gap_up_beyond_limit():
    con, store = synth_db(codes=3, seed=21)
    from krflow.data.models import Flow
    days = store.dates()
    for d in days[7:10]:
        store.upsert_flows([Flow("900000", d, 1e10, 1e10, 0, "KRW", True, "t", d, "x", f"{d} 18:00:00")])
    c = store.bar("900000", days[9])["close"]
    con.execute("UPDATE daily_bars SET open=?, high=? WHERE code='900000' AND date=?", (c * 1.10, c * 1.12, days[10]))
    con.commit()
    r = run_backtest(store, _settings(), days[9], days[30])   # 신호일부터 시작 -> 이전 보유 없음
    assert not [t for t in r.trades if t.code == "900000" and t.entry_date == days[10]]
    assert any(x["code"] == "900000" and "갭" in x["why"] for x in r.skipped)
