"""실행 설정. 실계좌 모드는 여기서부터 차단된다."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path


class RunMode(str, Enum):
    SYNTHETIC = "synthetic"   # 합성 데이터 테스트
    BACKTEST = "backtest"     # 실제 시장 데이터 백테스트
    PAPER = "paper"           # 내장 페이퍼 브로커 모의매매
    KIS_VTS = "kis_vts"       # 한국투자증권 모의투자 (미검증)
    REAL = "real"             # 실계좌 - 차단


class RealAccountBlocked(RuntimeError):
    pass


@dataclass
class CostModel:
    commission_rate: float = 0.00015   # 매수/매도 각각
    sell_tax_rate: float = 0.0015      # 매도세 (2025~ 코스피/코스닥 0.15%)
    slippage_rate: float = 0.001       # 체결가 불리 방향 가정


@dataclass
class StrategyParams:
    inst_streak_days: int = 3          # 기관 연속 순매수 일수
    require_foreign: bool = True       # 마지막 신호일 외국인 순매수 조건
    hold_days: int = 5                 # 매수일 포함 N번째 거래일 종가 매도
    rank_by: str = "inst_sum_ratio"    # inst_sum_ratio | inst_sum | foreign_net
    min_trading_value: float = 1e9     # 신호일 최소 거래대금 (원)


@dataclass
class RiskLimits:
    initial_cash: float = 10_000_000
    budget_per_trade: float = 1_000_000
    max_daily_buy: float = 3_000_000
    max_positions: int = 5
    max_loss_rate: float = 0.10        # 초기자금 대비 누적손실 도달 시 신규매수 중단


@dataclass
class Settings:
    mode: RunMode = RunMode.PAPER
    db_path: str = "data/krflow.db"
    costs: CostModel = field(default_factory=CostModel)
    strategy: StrategyParams = field(default_factory=StrategyParams)
    risk: RiskLimits = field(default_factory=RiskLimits)
    # 판단 시각: 신호일 수급 데이터는 이 시각까지 확인 가능해야 다음날 매매에 사용
    decision_time: str = "08:30"
    dashboard_port: int = 8765

    def __post_init__(self):
        if isinstance(self.mode, str):
            self.mode = RunMode(self.mode)
        if isinstance(self.costs, dict):
            self.costs = CostModel(**self.costs)
        if isinstance(self.strategy, dict):
            self.strategy = StrategyParams(**self.strategy)
        if isinstance(self.risk, dict):
            self.risk = RiskLimits(**self.risk)
        if self.mode == RunMode.REAL:
            raise RealAccountBlocked("실계좌 주문 모드는 이 프로그램에서 차단되어 있습니다.")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["mode"] = self.mode.value
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)

    @classmethod
    def load(cls, path: str | os.PathLike | None) -> "Settings":
        if path is None or not Path(path).exists():
            return cls()
        with open(path, encoding="utf-8") as f:
            return cls(**json.load(f))

    def save(self, path: str | os.PathLike) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_json())


def kis_credentials() -> dict:
    """환경변수에서만 읽는다. 값은 절대 로그/화면에 출력하지 않는다."""
    return {
        "app_key": os.environ.get("KIS_APP_KEY", ""),
        "app_secret": os.environ.get("KIS_APP_SECRET", ""),
        "account_no": os.environ.get("KIS_ACCOUNT_NO", ""),   # 예: 50123456-01
    }


def mask(value: str) -> str:
    if not value:
        return "(미설정)"
    return value[:2] + "*" * max(0, len(value) - 4) + value[-2:]
