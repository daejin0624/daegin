"""브로커 인터페이스와 주문 상태 정의."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class OrderState(str, Enum):
    NEW = "new"                          # 원장에 기록됨, 미전송
    SUBMITTED = "submitted"              # 접수
    UNFILLED = "unfilled"                # 미체결
    PARTIAL = "partial"                  # 부분체결
    FILLED = "filled"                    # 전량체결
    REJECTED = "rejected"                # 거절
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"                  # 전송 후 결과 불명 (타임아웃/통신장애) -> 조회로 해결 전 재주문 금지

    @property
    def is_open(self) -> bool:
        return self in (OrderState.SUBMITTED, OrderState.UNFILLED, OrderState.PARTIAL, OrderState.CANCEL_REQUESTED, OrderState.UNKNOWN, OrderState.NEW)


TERMINAL = {OrderState.FILLED, OrderState.REJECTED, OrderState.CANCELLED}


@dataclass
class Order:
    client_order_id: str
    code: str
    side: str                  # buy | sell
    qty: int
    order_type: str            # market | limit
    intent: str                # entry | exit
    scheduled_for: str         # 'YYYY-MM-DD open' / 'YYYY-MM-DD close'
    limit_price: float | None = None
    state: OrderState = OrderState.NEW
    broker_order_id: str | None = None
    filled_qty: int = 0
    avg_price: float | None = None
    reason: str = ""
    error: str | None = None
    signal_date: str | None = None
    rank: int | None = None


@dataclass
class OrderUpdate:
    state: OrderState
    filled_qty: int = 0
    avg_price: float | None = None
    broker_order_id: str | None = None
    error: str | None = None
    fills: list[tuple[int, float]] = field(default_factory=list)   # (qty, price)


@dataclass
class AccountSnapshot:
    cash: float                             # 예수금
    orderable_cash: float                   # 주문가능금액
    positions: dict[str, tuple[int, float]] # code -> (qty, avg_price)
    sellable: dict[str, int]                # code -> 매도가능수량
    asof: str
    verified_live: bool                     # 실제 증권사 연동으로 얻은 값인지


class BrokerError(Exception):
    pass


class BrokerTimeout(BrokerError):
    """전송 후 응답 없음. 주문이 접수됐을 수도 있으므로 UNKNOWN 처리."""


class Broker:
    name = "base"
    verified = False   # 실제 연동 검증 여부

    def submit(self, order: Order) -> OrderUpdate: raise NotImplementedError
    def query(self, order: Order) -> OrderUpdate: raise NotImplementedError
    def cancel(self, order: Order) -> OrderUpdate: raise NotImplementedError
    def account(self) -> AccountSnapshot: raise NotImplementedError
    def feature_status(self) -> dict[str, str]:
        """기능별 검증 상태: verified | unverified | unsupported"""
        return {}
