from krflow.config import Settings
from krflow.broker.base import Order, OrderState
from krflow.broker.ledger import OrderLedger, make_client_order_id
from krflow.broker.paper import PaperBroker
from krflow.ops.runner import Runner
from krflow.risk.limits import RiskManager
from helpers import synth_db


def _runner(con, store, **risk):
    s = Settings(mode="paper")
    s.strategy.min_trading_value = 0
    for k, v in risk.items():
        setattr(s.risk, k, v)
    broker = PaperBroker(store, s.costs, s.risk.initial_cash)
    return Runner(s, con, broker), broker


def test_client_order_id_is_deterministic():
    a = make_client_order_id("paper", "005930", "buy", "entry", "2025-01-06 open")
    assert a == make_client_order_id("paper", "005930", "buy", "entry", "2025-01-06 open")
    assert a != make_client_order_id("paper", "005930", "buy", "entry", "2025-01-07 open")


def test_ledger_never_resubmits_same_order():
    con, store = synth_db(codes=2)
    d = store.dates()[5]
    s = Settings(mode="paper")
    broker = PaperBroker(store, s.costs, 10_000_000)
    ledger = OrderLedger(con, "paper")
    o = Order(make_client_order_id("paper", "900000", "buy", "entry", f"{d} open"), "900000", "buy", 10, "market", "entry", f"{d} open")
    o1 = ledger.submit(broker, o)
    assert o1.state == OrderState.FILLED
    cash_after = broker.cash
    o2 = ledger.submit(broker, Order(o.client_order_id, "900000", "buy", 10, "market", "entry", f"{d} open"))
    assert o2.state == OrderState.FILLED and broker.cash == cash_after   # 재전송 없음


def test_timeout_marks_unknown_and_blocks_new_buys_until_resolved():
    con, store = synth_db(codes=10, seed=3)
    runner, broker = _runner(con, store)
    days = store.dates()
    runner.run_eod(days[9])
    sel = con.execute("SELECT COUNT(*) FROM candidates WHERE date=? AND selected=1", (days[9],)).fetchone()[0]
    if sel == 0:
        from krflow.data.models import Flow
        for d in days[7:10]:
            store.upsert_flows([Flow("900000", d, 1e9, 1e9, 0, "KRW", True, "t", d, "x", f"{d} 18:00:00")])
        runner._set_state("phases_done", {})
        runner.run_eod(days[9])
    broker.fail_next = "timeout"
    rep = runner.run_open(days[10])
    unk = runner.ledger.unknown_orders()
    assert len(unk) == 1 and any("결과 불명" in b for b in rep.blocked)
    # 위험관리: 결과 불명 주문 있으면 신규매수 차단
    d = runner.risk.evaluate(0, 0, 0, len(unk), 0, {}, 0)
    assert not d.allow_new_buys and d.allow_exits
    # 복구: 조회로 해결 -> 체결 반영, 포지션 생성, 잔고 일치
    notes = runner.recover()
    assert runner.ledger.unknown_orders() == []
    assert con.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1
    assert runner.unresolved_mismatches() == 0


def test_restart_does_not_duplicate_orders_or_positions():
    con, store = synth_db(codes=10, seed=4)
    runner, broker = _runner(con, store)
    days = store.dates()
    runner.simulate(days[5], days[20])
    n_orders = con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    n_pos = con.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    assert n_orders > 0
    # '재시작': 같은 DB로 새 Runner를 만들어 같은 날짜를 다시 실행
    runner2 = Runner(runner.s, con, broker)
    runner2.simulate(days[5], days[20])
    assert con.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == n_orders
    assert con.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == n_pos
    assert runner2.unresolved_mismatches() == 0
    # 페이퍼 브로커 현금과 원장 기록 일치
    acct = broker.account()
    assert abs(acct.cash - runner2._state("last_account")["cash"]) < 1e-6


def test_manual_halt_blocks_buys_but_allows_exits():
    con, store = synth_db(codes=10, seed=6)
    runner, broker = _runner(con, store, max_positions=2)
    days = store.dates()
    runner.simulate(days[5], days[15])
    n_pos_before = con.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    st = runner.risk.set_halt(True, "테스트 중단", "manual")
    assert st.halted and st.resumable
    n_orders = con.execute("SELECT COUNT(*) FROM orders WHERE side='buy'").fetchone()[0]
    runner.simulate(days[16], days[25])
    assert con.execute("SELECT COUNT(*) FROM orders WHERE side='buy'").fetchone()[0] == n_orders
    if n_pos_before:
        assert con.execute("SELECT COUNT(*) FROM orders WHERE side='sell'").fetchone()[0] > 0
    assert con.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
    runner.risk.resume()
    assert not runner.risk.halt_state().halted


def test_reconcile_detects_mismatch_and_blocks():
    con, store = synth_db(codes=5, seed=8)
    runner, broker = _runner(con, store)
    broker.positions["900004"] = (7, 1000.0)   # 증권사에만 있는 잔고
    notes = runner.reconcile()
    assert runner.unresolved_mismatches() == 1 and "900004" in notes[0]
    d = runner.risk.evaluate(0, 0, 0, 0, runner.unresolved_mismatches(), {}, 0)
    assert not d.allow_new_buys


def test_pending_buys_count_toward_limits():
    con, store = synth_db(codes=5)
    rm = RiskManager(con, Settings(mode="paper").risk)
    d = rm.evaluate(0, n_positions=3, n_pending_buys=2, unknown_orders=0, unresolved_mismatches=0, blocking_data_issues={}, daily_bought=0)
    assert not d.allow_new_buys
    qty, why = rm.buy_qty(10_000, orderable_cash=1_500_000, pending_buy_amount=1_000_000, daily_bought=0, commission_rate=0.00015)
    assert qty == 49 and why is None
