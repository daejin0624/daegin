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


def _force_signal(store, code, days):
    from krflow.data.models import Flow
    for d in days:
        store.upsert_flows([Flow(code, d, 1e10, 1e10, 0, "KRW", True, "t", d, "x", f"{d} 18:00:00")])


def test_gap_up_limit_entry_is_cancelled_at_sync():
    con, store = synth_db(codes=3, seed=21)
    runner, broker = _runner(con, store)
    days = store.dates()
    _force_signal(store, "900000", days[7:10])
    # 매수일 시가를 신호일 종가 대비 +10%로 만든다 (지정가 한도 +5% 초과)
    sig_close = store.bar("900000", days[9])["close"]
    con.execute("UPDATE daily_bars SET open=?, high=? WHERE code='900000' AND date=?", (sig_close * 1.10, sig_close * 1.12, days[10]))
    con.commit()
    runner.run_eod(days[9])
    rep = runner.run_open(days[10])
    o = [x for x in runner.ledger.all_orders() if x["code"] == "900000"][0]
    assert o["state"] == "unfilled" and o["order_type"] == "limit" and o["limit_price"] <= sig_close * 1.05
    rep = runner.run_sync(days[10], "open")
    assert runner.ledger.get(o["client_order_id"]).state == OrderState.CANCELLED
    assert not con.execute("SELECT 1 FROM positions WHERE code='900000'").fetchone()
    assert any("갭" in a for a in rep.actions)


def test_entry_sized_on_limit_price_within_budget():
    con, store = synth_db(codes=10, seed=4)
    runner, broker = _runner(con, store, budget_per_trade=700_000)
    days = store.dates()
    runner.simulate(days[5], days[40])
    for r in con.execute("SELECT qty, limit_price, avg_price FROM orders WHERE side='buy' AND state='filled'"):
        assert r["qty"] * r["limit_price"] <= 700_000
        assert r["avg_price"] <= r["limit_price"] * (1 + runner.s.costs.slippage_rate) + 1e-6


def test_partial_fill_then_cancel_creates_partial_position():
    con, store = synth_db(codes=3, seed=5)
    runner, broker = _runner(con, store)
    d = store.dates()[10]
    from krflow.broker.base import OrderUpdate
    o = Order(make_client_order_id("paper", "900001", "buy", "entry", f"{d} open"), "900001", "buy", 10, "limit", "entry", f"{d} open", limit_price=1e9)
    runner.ledger.record(o)
    runner.ledger.apply(o, OrderUpdate(OrderState.PARTIAL, 4, 1000.0, "B1"))
    broker._orders[o.client_order_id] = OrderUpdate(OrderState.PARTIAL, 4, 1000.0, "B1")
    broker.positions["900001"] = (4, 1000.0)
    runner.run_sync(d, "open")
    p = con.execute("SELECT * FROM positions WHERE code='900001'").fetchone()
    assert p is not None and p["qty"] == 4
    assert runner.ledger.get(o.client_order_id).state == OrderState.CANCELLED
    assert runner.unresolved_mismatches() == 0


def test_exit_on_halted_day_is_retried_next_day():
    con, store = synth_db(codes=3, seed=6)
    runner, broker = _runner(con, store)
    days = store.dates()
    d0 = days[10]
    exit_day = runner.cal.nth_trading_day_from(__import__("datetime").date.fromisoformat(d0), 5).isoformat()
    con.execute("INSERT INTO positions(code,qty,avg_price,entry_date,planned_exit_date) VALUES ('900002',5,1000,?,?)", (d0, exit_day))
    con.commit()
    broker.positions["900002"] = (5, 1000.0)
    con.execute("UPDATE daily_bars SET halted=1, open=NULL, open_tradable=0, close_tradable=0 WHERE code='900002' AND date=?", (exit_day,))
    con.commit()
    rep = runner.run_close(exit_day)
    assert any("거래정지" in b for b in rep.blocked)
    assert con.execute("SELECT exit_state FROM positions WHERE code='900002'").fetchone()[0] == "exit_blocked"
    nxt = days[days.index(exit_day) + 1]
    runner.run_close(nxt)
    assert con.execute("SELECT 1 FROM positions WHERE code='900002'").fetchone() is None
    c = con.execute("SELECT * FROM closed_positions WHERE code='900002'").fetchone()
    assert c["exit_date"] == nxt and "대신" in c["note"]
