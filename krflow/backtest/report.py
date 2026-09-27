"""결과 저장. 설정·데이터 지문·경고를 함께 저장해 재현 가능하게 한다."""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from .. import ENGINE_VERSION
from .engine import BacktestResult
from .metrics import summarize


def save_backtest(r: BacktestResult, out_dir: str | Path, label: str, data_kind: str) -> Path:
    """data_kind: synthetic | real_backtest | broker_paper — 화면과 결과물에서 구분한다."""
    out = Path(out_dir) / f"{datetime.now():%Y%m%d_%H%M%S}_{label}"
    out.mkdir(parents=True, exist_ok=True)
    meta = {
        "data_kind": data_kind,
        "engine_version": ENGINE_VERSION,
        "settings": r.settings,
        "data_fingerprint": r.data_fingerprint,
        "period": [r.start, r.end],
        "summary": summarize(r),
        "warnings": r.warnings,
        "disclaimer": ("합성 데이터 결과는 실제 투자 성과가 아니다. 실제 데이터 백테스트도 과거 결과이며 미래 수익을 보장하지 않는다."
                       if data_kind != "broker_paper" else "증권사 모의투자 결과. 실계좌 체결과 다를 수 있다."),
    }
    (out / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(out / "trades.csv", "w", newline="", encoding="utf-8-sig") as f:
        fields = ["code", "signal_date", "rank", "entry_date", "entry_price", "qty", "planned_exit_date", "exit_date", "exit_price", "fees", "tax", "pnl", "ret", "status", "notes", "reasons"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in r.trades:
            d = t.to_dict()
            d["notes"] = " | ".join(d["notes"])
            d["reasons"] = " | ".join(d["reasons"])
            w.writerow(d)
    with open(out / "equity.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["date", "cash", "market_value", "equity", "realized", "unrealized"])
        w.writeheader()
        w.writerows(r.equity_curve)
    (out / "candidates.json").write_text(json.dumps(r.candidates_log, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "skipped.json").write_text(json.dumps(r.skipped, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def save_json(obj, out_dir: str | Path, name: str) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"{datetime.now():%Y%m%d_%H%M%S}_{name}.json"
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return p
