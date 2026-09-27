"""주문 원장. 전송 전 기록(write-ahead) → 전송 → 결과 반영. 통신 장애 시 UNKNOWN으로 남겨 재주문을 막는다."""
from __future__ import annotations

import hashlib
import sqlite3

from ..data.models import now_iso
from .base import Broker, Order, OrderState, OrderUpdate, BrokerTimeout, BrokerError, TERMINAL


def make_client_order_id(run_mode: str, code: str, side: str, intent: str, scheduled_for: str) -> str:
    """같은 (모드, 종목, 방향, 의도, 예정시점)이면 항상 같은 ID -> 재시작/재실행 시 중복 주문 방지."""
    key = f"{run_mode}|{code}|{side}|{intent}|{scheduled_for}"
    return hashlib.sha1(key.encode()).hexdigest()[:20]


class OrderLedger:
    def __init__(self, con: sqlite3.Connection, run_mode: str):
        self.con = con
        self.run_mode = run_mode

    # ---- 조회 ----
    def get(self, client_order_id: str) -> Order | None:
        r = self.con.execute("SELECT * FROM orders WHERE client_order_id=?", (client_order_id,)).fetchone()
        return self._row_to_order(r) if r else None

    def open_orders(self, code: str | None = None) -> list[Order]:
        q = "SELECT * FROM orders WHERE run_mode=? AND state NOT IN ('filled','rejected','cancelled')"
        params: list = [self.run_mode]
        if code:
            q += " AND code=?"
            params.append(code)
        return [self._row_to_order(r) for r in self.con.execute(q + " ORDER BY scheduled_for", params)]

    def unknown_orders(self) -> list[Order]:
        return [self._row_to_order(r) for r in self.con.execute(
            "SELECT * FROM orders WHERE run_mode=? AND state='unknown'", (self.run_mode,))]

    def all_orders(self, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self.con.execute(
            "SELECT * FROM orders WHERE run_mode=? ORDER BY COALESCE(submitted_at, scheduled_for) DESC LIMIT ?", (self.run_mode, limit))]

    # ---- 쓰기 ----
    def record(self, o: Order) -> bool:
        """전송 전 기록. 이미 존재하면 False(중복 주문 시도)."""
        if self.get(o.client_order_id):
            return False
        self.con.execute(
            "INSERT INTO orders(client_order_id,broker_order_id,run_mode,code,side,qty,order_type,limit_price,state,filled_qty,avg_price,intent,scheduled_for,reason,signal_date,rank,last_update_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (o.client_order_id, None, self.run_mode, o.code, o.side, o.qty, o.order_type, o.limit_price, o.state.value, 0, None,
             o.intent, o.scheduled_for, o.reason, o.signal_date, o.rank, now_iso()))
        self.con.commit()
        return True

    def apply(self, o: Order, u: OrderUpdate) -> Order:
        prev_filled = o.filled_qty
        o.state = u.state
        o.filled_qty = max(o.filled_qty, u.filled_qty)
        o.avg_price = u.avg_price if u.avg_price is not None else o.avg_price
        o.broker_order_id = u.broker_order_id or o.broker_order_id
        o.error = u.error
        now = now_iso()
        self.con.execute(
            "UPDATE orders SET state=?, filled_qty=?, avg_price=?, broker_order_id=?, error=?, last_update_at=?, "
            "submitted_at=COALESCE(submitted_at, CASE WHEN ?<>'new' THEN ? END), filled_at=CASE WHEN ?='filled' THEN ? ELSE filled_at END WHERE client_order_id=?",
            (o.state.value, o.filled_qty, o.avg_price, o.broker_order_id, o.error, now, o.state.value, now, o.state.value, now, o.client_order_id))
        for qty, price in u.fills:
            self.con.execute("INSERT INTO fills(client_order_id,qty,price,filled_at) VALUES (?,?,?,?)", (o.client_order_id, qty, price, now))
        if not u.fills and o.filled_qty > prev_filled and o.avg_price:
            self.con.execute("INSERT INTO fills(client_order_id,qty,price,filled_at) VALUES (?,?,?,?)", (o.client_order_id, o.filled_qty - prev_filled, o.avg_price, now))
        self.con.commit()
        return o

    def submit(self, broker: Broker, o: Order) -> Order:
        """record -> submit. 타임아웃이면 UNKNOWN. 같은 ID가 이미 있으면 재전송하지 않는다."""
        if not self.record(o):
            existing = self.get(o.client_order_id)
            return existing  # type: ignore[return-value]
        try:
            u = broker.submit(o)
        except BrokerTimeout as e:
            u = OrderUpdate(OrderState.UNKNOWN, error=f"응답 없음: {e}")
        except BrokerError as e:
            u = OrderUpdate(OrderState.REJECTED, error=str(e))
        return self.apply(o, u)

    def resolve(self, broker: Broker, o: Order) -> Order:
        """미해결(UNKNOWN/미체결) 주문을 브로커 조회로 갱신."""
        try:
            u = broker.query(o)
        except BrokerError as e:
            o.error = f"조회 실패: {e}"
            return o
        return self.apply(o, u)

    def _row_to_order(self, r: sqlite3.Row) -> Order:
        return Order(r["client_order_id"], r["code"], r["side"], r["qty"], r["order_type"], r["intent"], r["scheduled_for"],
                     r["limit_price"], OrderState(r["state"]), r["broker_order_id"], r["filled_qty"], r["avg_price"],
                     r["reason"] or "", r["error"], r["signal_date"], r["rank"])
