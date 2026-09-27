"""전략 변형/비용/기간 비교. 모든 결과는 같은 데이터 지문과 설정을 함께 저장한다."""
from __future__ import annotations

import copy
import dataclasses

from ..config import Settings
from ..data.store import MarketStore
from .engine import run_backtest
from .metrics import summarize, benchmark_return


def _variant(settings: Settings, **strategy_overrides) -> Settings:
    s = copy.deepcopy(settings)
    for k, v in strategy_overrides.items():
        setattr(s.strategy, k, v)
    return s


def compare_strategies(store: MarketStore, settings: Settings, start: str, end: str) -> dict:
    variants = {
        "base(기관3일+외국인)": _variant(settings, require_foreign=True),
        "inst_only(기관3일 단독)": _variant(settings, require_foreign=False),
        "base+rank=inst_sum": _variant(settings, require_foreign=True, rank_by="inst_sum"),
        "base+rank=foreign_net": _variant(settings, require_foreign=True, rank_by="foreign_net"),
    }
    out = {name: summarize(run_backtest(store, s, start, end)) for name, s in variants.items()}
    base, inst = out["base(기관3일+외국인)"], out["inst_only(기관3일 단독)"]
    out["_effects"] = {
        "foreign_condition_effect": base["total_return"] - inst["total_return"],
        "rank_inst_sum_effect": out["base+rank=inst_sum"]["total_return"] - base["total_return"],
        "rank_foreign_net_effect": out["base+rank=foreign_net"]["total_return"] - base["total_return"],
    }
    out["_benchmark"] = benchmark_return(store, start, end)
    return out


def cost_sensitivity(store: MarketStore, settings: Settings, start: str, end: str) -> list[dict]:
    grid = [
        ("무비용", 0.0, 0.0, 0.0),
        ("수수료만", 0.00015, 0.0, 0.0),
        ("수수료+세금", 0.00015, 0.0015, 0.0),
        ("기본(수수료+세금+슬리피지0.1%)", 0.00015, 0.0015, 0.001),
        ("슬리피지 0.3%", 0.00015, 0.0015, 0.003),
        ("슬리피지 0.5%", 0.00015, 0.0015, 0.005),
    ]
    rows = []
    for name, com, tax, slip in grid:
        s = copy.deepcopy(settings)
        s.costs = dataclasses.replace(s.costs, commission_rate=com, sell_tax_rate=tax, slippage_rate=slip)
        m = summarize(run_backtest(store, s, start, end))
        rows.append({"scenario": name, "commission": com, "sell_tax": tax, "slippage": slip,
                     "total_return": m["total_return"], "max_drawdown": m["max_drawdown"], "win_rate": m["win_rate"], "trades": m["trades"], "avg_trade_return": m["avg_trade_return"]})
    return rows


def train_test(store: MarketStore, settings: Settings, start: str, end: str, split: str) -> dict:
    """학습기간(start~split)과 검증기간(split~end)을 분리해 같은 설정으로 실행. 학습기간에서 고른 설정을
    검증기간에 그대로 적용하는 것이 원칙이며, 검증기간 결과를 보고 설정을 바꾸면 검증이 아니다."""
    train = compare_strategies(store, settings, start, split)
    test = compare_strategies(store, settings, split, end)
    return {"train": {"start": start, "end": split, **train}, "test": {"start": split, "end": end, **test},
            "note": "검증기간 성과를 보고 설정을 바꾸지 말 것. 두 구간 모두에서 양(+)이어야 최소한의 신뢰가 있음"}
