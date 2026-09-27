from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Bar:
    code: str
    date: str          # YYYY-MM-DD
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    value: float | None            # 거래대금 (원)
    adj_factor: float = 1.0
    halted: bool = False
    open_tradable: bool = True
    close_tradable: bool = True
    source: str = "unknown"
    asof_date: str = ""
    collected_at: str = ""
    available_at: str = ""         # 이 데이터가 확인 가능해진 시각 (point-in-time 기준)
    revision: int = 0


@dataclass
class Flow:
    code: str
    date: str
    inst_net: float | None         # 기관 순매수 (unit 기준)
    foreign_net: float | None
    indiv_net: float | None = None
    unit: str = "KRW"
    is_final: bool = True
    source: str = "unknown"
    asof_date: str = ""
    collected_at: str = ""
    available_at: str = ""
    revision: int = 0


@dataclass
class CollectionResult:
    source: str
    dataset: str
    date_from: str
    date_to: str
    requested: int
    received: int
    status: str                    # ok | partial | failed | unsupported
    message: str = ""
    bars: list[Bar] = field(default_factory=list)
    flows: list[Flow] = field(default_factory=list)


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")
