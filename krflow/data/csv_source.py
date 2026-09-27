"""CSV 파일에서 실제 시장 데이터를 불러온다.

bars.csv  컬럼: code,date,open,high,low,close,volume,value[,adj_factor,halted,available_at]
flows.csv 컬럼: code,date,inst_net,foreign_net[,indiv_net,unit,is_final,available_at]
available_at이 없으면 '기준일 18:00'으로 가정한다(KRX 투자자별 매매동향 확정치 공개 시각 근사).
그 가정은 collection_log 메시지에 기록된다.
"""
from __future__ import annotations

import csv
from pathlib import Path

from .models import Bar, Flow, CollectionResult, now_iso

SOURCE = "csv"


def _f(v):
    v = (v or "").strip()
    return float(v) if v not in ("", "NA", "nan", "None") else None


def load(bars_path: str | Path, flows_path: str | Path | None) -> CollectionResult:
    bars: list[Bar] = []
    flows: list[Flow] = []
    now = now_iso()
    notes = []
    seen = set()
    dup = 0
    try:
        with open(bars_path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                key = (row["code"], row["date"])
                if key in seen:
                    dup += 1
                    continue
                seen.add(key)
                halted = row.get("halted", "0").strip() in ("1", "true", "True")
                bars.append(Bar(
                    row["code"].zfill(6), row["date"], _f(row.get("open")), _f(row.get("high")), _f(row.get("low")),
                    _f(row.get("close")), _f(row.get("volume")), _f(row.get("value")),
                    _f(row.get("adj_factor")) or 1.0, halted, not halted, not halted,
                    SOURCE, row["date"], now, row.get("available_at") or f"{row['date']} 18:00:00"))
    except FileNotFoundError as e:
        return CollectionResult(SOURCE, "bars", "", "", 0, 0, "failed", f"파일 없음: {e}")
    if dup:
        notes.append(f"중복행 {dup}건 무시")
    if flows_path:
        try:
            with open(flows_path, encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    flows.append(Flow(
                        row["code"].zfill(6), row["date"], _f(row.get("inst_net")), _f(row.get("foreign_net")),
                        _f(row.get("indiv_net")), row.get("unit") or "KRW",
                        (row.get("is_final") or "1").strip() in ("1", "true", "True"),
                        SOURCE, row["date"], now, row.get("available_at") or f"{row['date']} 18:00:00"))
        except FileNotFoundError as e:
            return CollectionResult(SOURCE, "bars+flows", "", "", 0, len(bars), "partial", f"수급 파일 없음: {e}", bars, [])
    if not bars:
        return CollectionResult(SOURCE, "bars+flows", "", "", 0, 0, "failed", "행 없음")
    dates = sorted({b.date for b in bars})
    notes.append("available_at 미지정 시 기준일 18:00 가정")
    status = "ok" if flows else "partial"
    return CollectionResult(SOURCE, "bars+flows", dates[0], dates[-1], len(bars), len(bars), status, "; ".join(notes), bars, flows)
