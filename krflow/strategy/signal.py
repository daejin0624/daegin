"""수급 신호. 신호일 T의 수급으로 T+1 시가 매수 후보를 만든다.

point-in-time 원칙: T+1 판단 시각(decision_time) 이전에 확인 가능(available_at)했던 수급만 사용한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

from ..config import StrategyParams
from ..data.store import MarketStore


@dataclass
class Candidate:
    code: str
    signal_date: str
    score: float
    rank: int | None
    selected: bool
    reasons: list[str] = field(default_factory=list)
    exclusion: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def compute_candidates(store: MarketStore, signal_date: str, params: StrategyParams,
                       decision_at: str, blocked: dict[str, str] | None = None) -> list[Candidate]:
    """signal_date 기준 후보. decision_at: 'YYYY-MM-DD HH:MM:SS' (다음 거래일 판단 시각).
    blocked: 데이터 이슈 등으로 제외할 종목 -> 사유. 반환은 선정(순위순) + 제외(사유 포함)."""
    blocked = blocked or {}
    n = params.inst_streak_days
    flows = store.flows_window(signal_date, n, available_before=decision_at)
    bars = store.bars_on(signal_date)
    passing: list[Candidate] = []
    rejected: list[Candidate] = []

    for code, rows in flows.items():
        reasons: list[str] = []
        if len(rows) < n:
            continue  # 수급 이력 부족 -> 후보 아님 (설명 대상에서 제외)
        last = rows[-1]
        if last["date"] != signal_date:
            continue
        inst_ok = all(r["inst_net"] is not None and r["inst_net"] > 0 for r in rows)
        if not inst_ok:
            continue  # 기관 연속 순매수 아님 -> 후보 아님
        streak = ", ".join(f"{r['inst_net']/1e8:+.1f}억" for r in rows)
        reasons.append(f"기관 {n}거래일 연속 순매수 ({streak})")
        excl = None
        if any(r["unit"] != "KRW" for r in rows):
            excl = "수급 단위 불일치"
        elif any(not r["is_final"] for r in rows):
            excl = "수급 잠정치 포함"
        elif params.require_foreign and not (last["foreign_net"] is not None and last["foreign_net"] > 0):
            excl = f"신호일 외국인 순매도 ({(last['foreign_net'] or 0)/1e8:+.1f}억)"
        elif params.require_foreign:
            reasons.append(f"신호일 외국인 순매수 ({last['foreign_net']/1e8:+.1f}억)")
        b = bars.get(code)
        if excl is None:
            if b is None:
                excl = "신호일 시세 없음"
            elif b["halted"]:
                excl = "거래정지"
            elif (b["value"] or 0) < params.min_trading_value:
                excl = f"거래대금 부족 ({(b['value'] or 0)/1e8:.1f}억 < {params.min_trading_value/1e8:.0f}억)"
            elif code in blocked:
                excl = f"데이터 이슈: {blocked[code]}"
        inst_sum = sum(r["inst_net"] for r in rows)
        if params.rank_by == "inst_sum_ratio":
            vals = [store.bar(code, r["date"]) for r in rows]
            tv = sum((v["value"] or 0) for v in vals if v is not None)
            score = inst_sum / tv if tv > 0 else 0.0
            reasons.append(f"순위점수: 3일 기관순매수/거래대금 = {score:.3f}")
        elif params.rank_by == "inst_sum":
            score = inst_sum
            reasons.append(f"순위점수: 3일 기관순매수 합 = {score/1e8:.1f}억")
        elif params.rank_by == "foreign_net":
            score = last["foreign_net"] or 0.0
            reasons.append(f"순위점수: 신호일 외국인 순매수 = {score/1e8:.1f}억")
        else:
            raise ValueError(f"unknown rank_by: {params.rank_by}")
        c = Candidate(code, signal_date, score, None, excl is None, reasons, excl)
        (passing if excl is None else rejected).append(c)

    passing.sort(key=lambda c: (-c.score, c.code))
    for i, c in enumerate(passing, 1):
        c.rank = i
    return passing + rejected


def persist_candidates(store: MarketStore, cands: list[Candidate], computed_at: str) -> None:
    import json
    store.con.executemany(
        "INSERT OR REPLACE INTO candidates(date,code,rank,selected,score,reasons,exclusion,computed_at) VALUES (?,?,?,?,?,?,?,?)",
        [(c.signal_date, c.code, c.rank, int(c.selected), c.score, json.dumps(c.reasons, ensure_ascii=False), c.exclusion, computed_at) for c in cands])
    store.con.commit()
