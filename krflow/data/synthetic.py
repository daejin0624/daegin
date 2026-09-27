"""합성 데이터 생성기. 시드로 완전히 재현된다. 이 데이터의 수익률은 실제 투자 성과와 무관하다."""
from __future__ import annotations

import math
import random
from datetime import date, timedelta

from ..calendar import TradingCalendar
from .models import Bar, Flow, CollectionResult

SOURCE = "synthetic"


def generate(start: date, end: date, n_codes: int = 30, seed: int = 42, flow_effect: float = 0.0,
             cal: TradingCalendar | None = None, inject_issues: bool = False) -> CollectionResult:
    """flow_effect: 기관 순매수가 다음날 수익률에 미치는 가상의 효과(0이면 수급과 수익률 무관).
    이 값을 바꿔가며 엔진이 효과를 올바르게 탐지하는지 검증하는 용도로만 쓴다."""
    rng = random.Random(seed)
    cal = cal or TradingCalendar()
    days = cal.trading_days(start, end)
    bars: list[Bar] = []
    flows: list[Flow] = []
    codes = [f"{900000 + i:06d}" for i in range(n_codes)]
    for ci, code in enumerate(codes):
        price = rng.uniform(5_000, 200_000)
        vol_sigma = rng.uniform(0.015, 0.035)
        adv = rng.uniform(2e9, 5e10)  # 일평균 거래대금
        inst_prev = 0.0
        pending_effect = 0.0
        for di, d in enumerate(days):
            ds = d.isoformat()
            halted = inject_issues and ci == 1 and 40 <= di < 43
            ret = rng.gauss(vol_sigma ** 2 / 2, vol_sigma) + pending_effect   # 변동성 손실 보정(기대 드리프트 0)
            ret = max(-0.30, min(0.30, ret))
            o = price * (1 + rng.gauss(0, vol_sigma / 3))
            c = price * (1 + ret)
            hi = max(o, c) * (1 + abs(rng.gauss(0, vol_sigma / 3)))
            lo = min(o, c) * (1 - abs(rng.gauss(0, vol_sigma / 3)))
            value = adv * math.exp(rng.gauss(0, 0.5))
            volume = value / max(c, 1)
            # 수급: 자기상관이 있는 기관 순매수 (연속 순매수가 자연스럽게 발생하도록)
            inst = 0.6 * inst_prev + rng.gauss(0, 0.05 * value)
            foreign = rng.gauss(0, 0.05 * value) + 0.2 * inst
            indiv = -(inst + foreign) + rng.gauss(0, 0.02 * value)
            inst_prev = inst
            pending_effect = flow_effect * (inst / value) if value else 0.0
            if halted:
                bars.append(Bar(code, ds, None, None, None, price, 0, 0, 1.0, True, False, False, SOURCE, ds, f"{ds} 18:00:00", f"{ds} 18:00:00"))
                flows.append(Flow(code, ds, 0.0, 0.0, 0.0, "KRW", True, SOURCE, ds, f"{ds} 18:00:00", f"{ds} 18:00:00"))
                continue
            price = c
            bars.append(Bar(code, ds, round(o, 0), round(hi, 0), round(lo, 0), round(c, 0), round(volume), round(value),
                            1.0, False, True, True, SOURCE, ds, f"{ds} 18:00:00", f"{ds} 18:00:00"))
            flows.append(Flow(code, ds, round(inst), round(foreign), round(indiv), "KRW", True, SOURCE, ds,
                              f"{ds} 18:00:00", f"{ds} 18:00:00"))
    if inject_issues and len(days) > 30:
        # 종목 2: 수급 잠정치, 종목 3: 단위 불일치, 종목 4: 날짜 하나 시세 누락, 종목 5: 지연 공개
        d20 = days[20].isoformat()
        for f in flows:
            if f.code == codes[2] and f.date == d20:
                f.is_final = False
            if f.code == codes[3] and f.date == d20:
                f.unit = "shares"
            if f.code == codes[5] and f.date == d20:
                f.available_at = (days[20] + timedelta(days=7)).isoformat() + " 18:00:00"
        bars = [b for b in bars if not (b.code == codes[4] and b.date == d20)]
    return CollectionResult(SOURCE, "bars+flows", days[0].isoformat(), days[-1].isoformat(),
                            n_codes * len(days), len(bars), "ok", "합성 데이터", bars, flows)
