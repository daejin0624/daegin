"""내장 페이퍼 브로커. 저장된 시세로 시가/종가 체결을 시뮬레이션한다. 장애 주입으로 원장 로직을 검증할 수 있다."""
from __future__ import annotations

from ..config import CostModel
from ..data.store import MarketStore
from .base import Broker, Order, OrderUpdate, OrderState, AccountSnapshot, BrokerTimeout, BrokerError, Quote


class PaperBroker(Broker):
    name = "paper"
    verified = True   # 내부 시뮬레이션이므로 '검증'의 의미는 단위테스트 통과

    def __init__(self, store: MarketStore, costs: CostModel, initial_cash: float):
        self.store = store
        self.costs = costs
        self.cash = initial_cash
        self.positions: dict[str, tuple[int, float]] = {}
        self._orders: dict[str, OrderUpdate] = {}
        self.fail_next: str | None = None   # 'timeout' | 'reject' 테스트용 장애 주입

    def _price(self, o: Order) -> float | None:
        date, phase = o.scheduled_for.split(" ")
        b = self.store.bar(o.code, date)
        if b is None or b["halted"]:
            return None
        if phase == "open":
            return None if not b["open_tradable"] or not b["open"] else b["open"]
        return None if not b["close_tradable"] or not b["close"] else b["close"]

    def _fill(self, o: Order) -> OrderUpdate:
        px = self._price(o)
        if px is None:
            return OrderUpdate(OrderState.REJECTED, error="거래 불가(정지/시세 없음)", broker_order_id=f"P{o.client_order_id[:8]}")
        bid = f"P{o.client_order_id[:8]}"
        if o.order_type == "limit" and o.side == "buy" and px > (o.limit_price or 0):
            return OrderUpdate(OrderState.UNFILLED, broker_order_id=bid)   # 시가가 지정가 초과 -> 미체결
        slip = self.costs.slippage_rate
        px = px * (1 + slip) if o.side == "buy" else px * (1 - slip)
        if o.side == "buy":
            cost = px * o.qty * (1 + self.costs.commission_rate)
            if cost > self.cash:
                return OrderUpdate(OrderState.REJECTED, error="주문가능금액 부족", broker_order_id=f"P{o.client_order_id[:8]}")
            self.cash -= cost
            q, a = self.positions.get(o.code, (0, 0.0))
            nq = q + o.qty
            self.positions[o.code] = (nq, (q * a + o.qty * px) / nq)
        else:
            q, a = self.positions.get(o.code, (0, 0.0))
            if q < o.qty:
                return OrderUpdate(OrderState.REJECTED, error=f"매도가능수량 부족({q}<{o.qty})", broker_order_id=f"P{o.client_order_id[:8]}")
            self.cash += px * o.qty * (1 - self.costs.commission_rate - self.costs.sell_tax_rate)
            self.positions[o.code] = (q - o.qty, a)
            if q - o.qty == 0:
                del self.positions[o.code]
        return OrderUpdate(OrderState.FILLED, o.qty, px, broker_order_id=f"P{o.client_order_id[:8]}", fills=[(o.qty, px)])

    def submit(self, o: Order) -> OrderUpdate:
        if self.fail_next == "reject":
            self.fail_next = None
            raise BrokerError("테스트 거절")
        u = self._fill(o)
        self._orders[o.client_order_id] = u
        if self.fail_next == "timeout":
            self.fail_next = None
            raise BrokerTimeout("테스트 타임아웃 (브로커는 체결 처리함)")
        return u

    def query(self, o: Order) -> OrderUpdate:
        u = self._orders.get(o.client_order_id)
        if u is None:
            return OrderUpdate(OrderState.REJECTED, error="브로커에 주문 없음")
        return OrderUpdate(u.state, u.filled_qty, u.avg_price, u.broker_order_id, u.error)  # fills는 이미 반영됨

    def cancel(self, o: Order) -> OrderUpdate:
        u = OrderUpdate(OrderState.CANCELLED, o.filled_qty, o.avg_price, broker_order_id=o.broker_order_id)
        self._orders[o.client_order_id] = u
        return u

    def quote(self, code: str, date: str) -> Quote:
        from ..data.models import now_iso
        prev = self.store.con.execute("SELECT close FROM daily_bars WHERE code=? AND date<? AND close IS NOT NULL ORDER BY date DESC LIMIT 1", (code, date)).fetchone()
        b = self.store.bar(code, date)
        halted = b is None or bool(b["halted"])
        return Quote(code, prev[0] if prev else None, halted, now_iso(), "paper(저장 시세)")

    def account(self) -> AccountSnapshot:
        from ..data.models import now_iso
        return AccountSnapshot(self.cash, self.cash, dict(self.positions), {c: q for c, (q, _) in self.positions.items()}, now_iso(), False)

    def feature_status(self) -> dict[str, str]:
        return {"submit": "verified(단위테스트)", "query": "verified(단위테스트)", "cancel": "verified(단위테스트)", "account": "verified(단위테스트)"}
