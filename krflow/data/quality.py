"""데이터 품질 검사. 문제가 있는 (종목, 날짜)는 data_issues에 기록되고 매매에서 제외된다."""
from __future__ import annotations

import sqlite3

from .models import now_iso

# kind -> 매매 차단 여부
ISSUE_KINDS = {
    "missing_bar": True,        # 거래일인데 시세 없음
    "missing_flow": True,       # 시세는 있는데 수급 없음
    "duplicate": True,          # 동일 키 중복 (INSERT OR REPLACE로 방지되므로 원천 파일 검사용)
    "abnormal_price": True,     # 고가<저가, 음수, 0, 시가/종가가 고저 범위 밖
    "abnormal_jump": True,      # 전일 대비 ±35% 초과 (상하한가 30% + 여유) — 분할/병합 미반영 의심
    "unit_mismatch": True,      # 수급 단위가 KRW가 아님
    "provisional_flow": True,   # 잠정치 수급
    "stale": True,              # 확인 가능 시각이 데이터 기준일보다 비정상적으로 늦음 (지연)
    "revised": False,           # 정정 데이터 (정보 표시, 차단 안 함)
    "unadjusted_ca": True,      # 기업행동 있는데 수정계수 1.0
}


def run_quality_checks(con: sqlite3.Connection, dates: list[str] | None = None) -> list[dict]:
    """검사 후 새로 발견된 이슈 목록을 반환하고 data_issues에 기록한다."""
    issues: list[dict] = []
    if dates is None:
        dates = [r[0] for r in con.execute("SELECT DISTINCT date FROM daily_bars ORDER BY date")]
    # 종목별 데이터 존재 구간(상장~폐지). 구간 밖 날짜는 누락으로 보지 않는다.
    span = {r[0]: (r[1], r[2]) for r in con.execute("SELECT code, MIN(date), MAX(date) FROM daily_bars GROUP BY code")}
    codes = list(span)
    date_set = set(dates)

    prev_close: dict[str, float] = {}
    for d in dates:
        bars = {r["code"]: r for r in con.execute("SELECT * FROM daily_bars WHERE date=?", (d,))}
        flows = {r["code"]: r for r in con.execute("SELECT * FROM investor_flows WHERE date=?", (d,))}
        cas = {r["code"]: r for r in con.execute("SELECT * FROM corporate_actions WHERE date=?", (d,))}
        for c in codes:
            b = bars.get(c)
            if b is None and not (span[c][0] <= d <= span[c][1]):
                continue
            if b is None:
                issues.append(_issue(c, d, "daily_bars", "missing_bar", "거래일 시세 누락"))
                continue
            if b["halted"]:
                prev_close[c] = b["close"] if b["close"] else prev_close.get(c, 0)
                continue
            vals = (b["open"], b["high"], b["low"], b["close"])
            if any(v is None or v <= 0 for v in vals) or b["high"] < b["low"] or not (b["low"] <= b["open"] <= b["high"]) or not (b["low"] <= b["close"] <= b["high"]):
                issues.append(_issue(c, d, "daily_bars", "abnormal_price", f"OHLC={vals}"))
            elif c in prev_close and prev_close[c] > 0:
                chg = b["close"] / prev_close[c] - 1
                if abs(chg) > 0.35 and c not in cas:
                    issues.append(_issue(c, d, "daily_bars", "abnormal_jump", f"전일대비 {chg:+.1%}, 기업행동 없음"))
            if c in cas and abs(b["adj_factor"] - 1.0) < 1e-12 and cas[c]["kind"] in ("split", "merge"):
                issues.append(_issue(c, d, "daily_bars", "unadjusted_ca", f"{cas[c]['kind']} ratio={cas[c]['ratio']} 수정계수 미반영"))
            if b["revision"] > 0:
                issues.append(_issue(c, d, "daily_bars", "revised", f"정정 {b['revision']}회"))
            if b["close"]:
                prev_close[c] = b["close"]

            f = flows.get(c)
            if f is None:
                issues.append(_issue(c, d, "investor_flows", "missing_flow", "수급 누락"))
                continue
            if f["unit"] != "KRW":
                issues.append(_issue(c, d, "investor_flows", "unit_mismatch", f"unit={f['unit']}"))
            if not f["is_final"]:
                issues.append(_issue(c, d, "investor_flows", "provisional_flow", "잠정치"))
            if f["available_at"] and f["available_at"][:10] > d and (_days_between(d, f["available_at"][:10]) > 3):
                issues.append(_issue(c, d, "investor_flows", "stale", f"확인가능 {f['available_at']}"))
            if f["revision"] > 0:
                issues.append(_issue(c, d, "investor_flows", "revised", f"정정 {f['revision']}회"))

    # 중복 기록 (기본키로 막히므로 실제 발생하면 스키마 훼손)
    for tbl in ("daily_bars", "investor_flows"):
        dup = con.execute(f"SELECT code,date,COUNT(*) n FROM {tbl} GROUP BY code,date HAVING n>1").fetchall()
        for r in dup:
            issues.append(_issue(r["code"], r["date"], tbl, "duplicate", f"{r['n']}건"))

    # 기존 미해결 이슈와 중복 방지
    existing = {(r["code"], r["date"], r["dataset"], r["kind"]) for r in con.execute("SELECT code,date,dataset,kind FROM data_issues WHERE resolved_at IS NULL")}
    new = [i for i in issues if (i["code"], i["date"], i["dataset"], i["kind"]) not in existing]
    con.executemany(
        "INSERT INTO data_issues(code,date,dataset,kind,detail,blocks_trading,detected_at) VALUES (?,?,?,?,?,?,?)",
        [(i["code"], i["date"], i["dataset"], i["kind"], i["detail"], int(ISSUE_KINDS[i["kind"]]), now_iso()) for i in new])
    con.commit()
    return new


def blocked_codes(con: sqlite3.Connection, date: str) -> dict[str, str]:
    """해당 날짜에 매매 차단 사유가 있는 종목 -> 사유."""
    out: dict[str, str] = {}
    for r in con.execute("SELECT code,kind,detail FROM data_issues WHERE resolved_at IS NULL AND blocks_trading=1 AND date=?", (date,)):
        out.setdefault(r["code"], f"{r['kind']}: {r['detail']}")
    return out


def _issue(code, date, dataset, kind, detail) -> dict:
    return {"code": code, "date": date, "dataset": dataset, "kind": kind, "detail": detail}


def _days_between(a: str, b: str) -> int:
    from datetime import date
    return (date.fromisoformat(b) - date.fromisoformat(a)).days
