"""백테스트 엔진. 거래일 단위 이벤트 루프.

- 신호일 T의 후보를 T+1 시가에 매수 (판단 시각 이전 확인 가능 데이터만 사용)
- 매수일 포함 hold_days번째 거래일 종가에 매도
- 시가/종가 거래 불가(정지)면 다음 거래일로 이월하고 상태를 기록
- 동일 종목 중복 보유 금지, 미체결(예정) 매수도 보유 한도에 포함
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict

from ..calendar import TradingCalendar
from ..config import Settings
from ..data.store import MarketStore
from ..data.quality import blocked_codes
from ..strategy.signal import compute_candidates, Candidate


@dataclass
class Trade:
    code: str
    signal_date: str
    rank: int
    entry_date: str
    entry_price: float           # 슬리피지 포함 체결가
    qty: int
    planned_exit_date: str
    exit_date: str | None = None
    exit_price: float | None = None
    fees: float = 0.0
    tax: float = 0.0
    pnl: float | None = None
    ret: float | None = None
    status: str = "open"         # open | closed | exit_blocked
    notes: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BacktestResult:
    settings: dict
    start: str
    end: str
    data_fingerprint: str
    trades: list[Trade]
    equity_curve: list[dict]              # date, cash, market_value, equity, realized, unrealized
    candidates_log: dict[str, list[dict]] # signal_date -> candidates
    skipped: list[dict]                   # 매수하지 않은 선정 후보와 사유
    final_cash: float
    open_positions: list[dict]
    warnings: list[str]


def run_backtest(store: MarketStore, settings: Settings, start: str, end: str,
                 warnings: list[str] | None = None, cal: TradingCalendar | None = None) -> BacktestResult:
    p, costs, risk = settings.strategy, settings.costs, settings.risk
    days = [d for d in store.dates() if start <= d <= end]
    all_days = store.dates()
    if len(days) < p.inst_streak_days + p.hold_days:
        raise ValueError("기간이 너무 짧습니다")
    cash = risk.initial_cash
    realized = 0.0
    positions: dict[str, Trade] = {}
    trades: list[Trade] = []
    equity_curve: list[dict] = []
    cand_log: dict[str, list[dict]] = {}
    skipped: list[dict] = []
    warnings = list(warnings or [])
    warnings.append("과거 데이터 백테스트: 실제 체결 가능성, 호가 유동성, 주문 지연은 반영되지 않음")
    warnings.append("유니버스에 상장폐지 종목이 빠져 있으면 생존편향이 있음 (universe 테이블 확인)")

    cal = cal or TradingCalendar()

    def exit_date_for(entry_date: str) -> str:
        i = all_days.index(entry_date)
        j = i + p.hold_days - 1
        if j < len(all_days):
            return all_days[j]
        # 데이터 범위를 넘으면 달력으로 계산 -> 기간 말 미청산 포지션으로 남는다
        from datetime import date as _date
        return cal.next_trading_day(_date.fromisoformat(all_days[-1]), j - len(all_days) + 1).isoformat()

    for di, d in enumerate(days):
        bars = store.bars_on(d)
        blocked = blocked_codes(store.con, d)
        # 1) 신규 매수 (시가): 전 거래일 신호. 당일 종가 청산 예정 종목도 아직 보유 중이므로 제외
        if di > 0:
            sig = days[di - 1]
            decision_at = f"{d} {settings.decision_time}:00"
            cands = compute_candidates(store, sig, p, decision_at, blocked_codes(store.con, sig))
            cand_log[sig] = [c.to_dict() for c in cands]
            daily_bought = 0.0
            loss_halt = (realized <= -risk.max_loss_rate * risk.initial_cash)
            for c in cands:
                if not c.selected:
                    continue
                why = None
                b = bars.get(c.code)
                if loss_halt:
                    why = "누적손실 한도 도달로 신규매수 중단"
                elif c.code in positions:
                    why = "이미 보유 중 (중복 보유 금지)"
                elif len(positions) >= risk.max_positions:
                    why = f"최대 보유 종목 수 {risk.max_positions} 도달"
                elif b is None or not b["open_tradable"] or b["open"] is None:
                    why = "매수일 시가 거래 불가(정지/시세 없음)"
                elif c.code in blocked:
                    why = f"매수일 데이터 이슈: {blocked[c.code]}"
                elif daily_bought + risk.budget_per_trade > risk.max_daily_buy:
                    why = f"일별 매수 한도 {risk.max_daily_buy:,.0f} 도달"
                if why is None:
                    px = b["open"] * (1 + costs.slippage_rate)
                    qty = math.floor(min(risk.budget_per_trade, cash / (1 + costs.commission_rate)) / px)
                    if qty <= 0:
                        why = "현금 부족 또는 1주 미만"
                if why is not None:
                    skipped.append({"date": d, "code": c.code, "rank": c.rank, "why": why})
                    continue
                fee = px * qty * costs.commission_rate
                cash -= px * qty + fee
                daily_bought += px * qty
                t = Trade(c.code, sig, c.rank, d, px, qty, exit_date_for(d), fees=fee, reasons=c.reasons)
                positions[c.code] = t
                trades.append(t)

        # 2) 예정 청산 (종가) — 시가 매수 뒤에 처리해 같은 날 청산 종목을 재매수하지 않는다
        for code, t in list(positions.items()):
            if t.planned_exit_date > d:
                continue
            b = bars.get(code)
            if b is None or not b["close_tradable"] or b["close"] is None or code in blocked:
                why = "거래정지" if (b is not None and b["halted"]) else ("시세 없음" if b is None else f"데이터 이슈: {blocked.get(code, '종가 거래불가')}")
                t.status = "exit_blocked"
                t.notes.append(f"{d} 종가 매도 불가({why}) -> 다음 거래일 재시도")
                continue
            px = b["close"] * (1 - costs.slippage_rate)
            fee = px * t.qty * costs.commission_rate
            tax = px * t.qty * costs.sell_tax_rate
            cash += px * t.qty - fee - tax
            t.exit_date, t.exit_price = d, px
            t.fees += fee
            t.tax += tax
            gross = (px - t.entry_price) * t.qty
            t.pnl = gross - t.fees - t.tax
            t.ret = t.pnl / (t.entry_price * t.qty)
            t.status = "closed"
            if d != t.planned_exit_date:
                t.notes.append(f"예정일 {t.planned_exit_date} 대신 {d} 청산")
            realized += t.pnl
            del positions[code]

        # 3) 일별 평가
        mv = 0.0
        unreal = 0.0
        for code, t in positions.items():
            b = bars.get(code)
            px = b["close"] if b is not None and b["close"] else t.entry_price
            mv += px * t.qty
            unreal += (px - t.entry_price) * t.qty
        equity_curve.append({"date": d, "cash": cash, "market_value": mv, "equity": cash + mv,
                             "realized": realized, "unrealized": unreal})

    return BacktestResult(settings.to_dict(), days[0], days[-1], store.fingerprint(), trades, equity_curve,
                          cand_log, skipped, cash, [t.to_dict() for t in positions.values()], warnings)
