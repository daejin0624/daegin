from __future__ import annotations

from .engine import BacktestResult


def summarize(r: BacktestResult) -> dict:
    eq = [e["equity"] for e in r.equity_curve]
    initial = r.settings["risk"]["initial_cash"]
    peak, mdd = -1e18, 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    closed = [t for t in r.trades if t.status == "closed"]
    wins = [t for t in closed if (t.pnl or 0) > 0]
    rets = [t.ret for t in closed if t.ret is not None]
    return {
        "start": r.start, "end": r.end,
        "initial_cash": initial,
        "final_equity": eq[-1] if eq else initial,
        "total_return": (eq[-1] / initial - 1) if eq else 0.0,
        "max_drawdown": mdd,
        "trades": len(r.trades), "closed_trades": len(closed),
        "open_positions": len(r.open_positions),
        "exit_blocked": sum(1 for t in r.trades if t.status == "exit_blocked"),
        "win_rate": (len(wins) / len(closed)) if closed else None,
        "avg_trade_return": (sum(rets) / len(rets)) if rets else None,
        "median_trade_return": (sorted(rets)[len(rets) // 2]) if rets else None,
        "realized_pnl": r.equity_curve[-1]["realized"] if r.equity_curve else 0.0,
        "unrealized_pnl": r.equity_curve[-1]["unrealized"] if r.equity_curve else 0.0,
        "final_cash": r.final_cash,
        "total_fees": sum(t.fees for t in r.trades), "total_tax": sum(t.tax for t in r.trades),
        "skipped": len(r.skipped),
        "data_fingerprint": r.data_fingerprint,
    }


def benchmark_return(store, start: str, end: str, code: str | None = None) -> dict:
    """벤치마크: code가 있으면 그 종목/지수 시리즈의 보유수익, 없으면 유니버스 동일가중 보유수익."""
    days = [d for d in store.dates() if start <= d <= end]
    if not days:
        return {"kind": "none", "return": None}
    if code:
        a, b = store.bar(code, days[0]), store.bar(code, days[-1])
        if a and b and a["close"] and b["close"]:
            return {"kind": f"index:{code}", "return": b["close"] / a["close"] - 1}
        return {"kind": f"index:{code}", "return": None, "note": "벤치마크 시세 없음"}
    first, last = store.bars_on(days[0]), store.bars_on(days[-1])
    rets = [last[c]["close"] / first[c]["close"] - 1 for c in first if c in last and first[c]["close"] and last[c]["close"]]
    return {"kind": "equal_weight_universe", "return": sum(rets) / len(rets) if rets else None, "n": len(rets),
            "note": "유니버스 동일가중 보유(비용 미반영)"}
