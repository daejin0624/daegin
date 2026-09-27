"""위험관리·운영 통제. 신규 매수 차단과 기존 보유분 청산 가능 여부를 분리해서 판단한다.
AI/분석 설명은 이 모듈의 판단에 관여할 수 없다(입력이 없음)."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from ..config import RiskLimits
from ..data.models import now_iso


@dataclass
class HaltState:
    halted: bool = False
    reason: str = ""
    kind: str = ""            # manual | loss | system
    resumable: bool = True
    since: str = ""


@dataclass
class RiskDecision:
    allow_new_buys: bool
    allow_exits: bool
    reasons: list[str] = field(default_factory=list)   # 차단 사유
    per_code_block: dict[str, str] = field(default_factory=dict)


class RiskManager:
    def __init__(self, con: sqlite3.Connection, limits: RiskLimits):
        self.con = con
        self.limits = limits

    # ---- 중단 상태 ----
    def halt_state(self) -> HaltState:
        r = self.con.execute("SELECT value FROM ops_state WHERE key='halt'").fetchone()
        return HaltState(**json.loads(r[0])) if r else HaltState()

    def set_halt(self, halted: bool, reason: str = "", kind: str = "manual", resumable: bool = True) -> HaltState:
        st = HaltState(halted, reason, kind if halted else "", resumable, now_iso() if halted else "")
        self.con.execute("INSERT OR REPLACE INTO ops_state(key,value,updated_at) VALUES ('halt',?,?)", (json.dumps(st.__dict__, ensure_ascii=False), now_iso()))
        self.con.commit()
        return st

    def resume(self) -> HaltState:
        st = self.halt_state()
        if st.halted and not st.resumable:
            return st
        return self.set_halt(False)

    # ---- 판단 ----
    def evaluate(self, realized_pnl: float, n_positions: int, n_pending_buys: int, unknown_orders: int,
                 unresolved_mismatches: int, blocking_data_issues: dict[str, str], daily_bought: float) -> RiskDecision:
        d = RiskDecision(True, True, per_code_block=dict(blocking_data_issues))
        st = self.halt_state()
        if st.halted:
            d.allow_new_buys = False
            d.reasons.append(f"중단 상태({st.kind}): {st.reason}")
        if realized_pnl <= -self.limits.max_loss_rate * self.limits.initial_cash:
            d.allow_new_buys = False
            d.reasons.append(f"누적 실현손실 {realized_pnl:,.0f} ≤ 한도 -{self.limits.max_loss_rate:.0%}")
            if not st.halted:
                self.set_halt(True, "손실 한도 도달", "loss", resumable=True)
        if unknown_orders > 0:
            d.allow_new_buys = False
            d.reasons.append(f"결과 불명 주문 {unknown_orders}건 미해결")
        if unresolved_mismatches > 0:
            d.allow_new_buys = False
            d.reasons.append(f"잔고 불일치 {unresolved_mismatches}건 미해결")
        if n_positions + n_pending_buys >= self.limits.max_positions:
            d.allow_new_buys = False
            d.reasons.append(f"보유+예정 {n_positions + n_pending_buys} ≥ 최대 {self.limits.max_positions}")
        if daily_bought >= self.limits.max_daily_buy:
            d.allow_new_buys = False
            d.reasons.append("일별 매수 한도 도달")
        return d

    def buy_qty(self, price: float, orderable_cash: float, pending_buy_amount: float, daily_bought: float, commission_rate: float) -> tuple[int, str | None]:
        """미체결 매수 예정금액을 주문가능금액에서 차감해 수량 계산."""
        avail = min(self.limits.budget_per_trade, orderable_cash - pending_buy_amount, self.limits.max_daily_buy - daily_bought)
        if avail <= 0:
            return 0, "주문가능금액 부족(미체결 포함)"
        qty = int(avail / (price * (1 + commission_rate)))
        return (qty, None) if qty > 0 else (0, "1주 미만")
