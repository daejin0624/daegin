"""시장 데이터 저장/조회. 모든 조회는 point-in-time(확인 가능 시각) 필터를 지원한다."""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import astuple

from .models import Bar, Flow, CollectionResult, now_iso


class MarketStore:
    def __init__(self, con: sqlite3.Connection):
        self.con = con

    # ---- 쓰기 ----
    def upsert_bars(self, bars: list[Bar]) -> int:
        rows = []
        for b in bars:
            prev = self.con.execute("SELECT close, revision FROM daily_bars WHERE code=? AND date=?", (b.code, b.date)).fetchone()
            rev = b.revision
            if prev is not None and prev["close"] != b.close:
                rev = prev["revision"] + 1  # 정정 데이터
            rows.append((b.code, b.date, b.open, b.high, b.low, b.close, b.volume, b.value, b.adj_factor,
                         int(b.halted), int(b.open_tradable), int(b.close_tradable), b.source, b.asof_date,
                         b.collected_at, b.available_at, rev))
        self.con.executemany("INSERT OR REPLACE INTO daily_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        self.con.commit()
        return len(rows)

    def upsert_flows(self, flows: list[Flow]) -> int:
        rows = []
        for f in flows:
            prev = self.con.execute("SELECT inst_net, foreign_net, revision FROM investor_flows WHERE code=? AND date=?", (f.code, f.date)).fetchone()
            rev = f.revision
            if prev is not None and (prev["inst_net"] != f.inst_net or prev["foreign_net"] != f.foreign_net):
                rev = prev["revision"] + 1
            rows.append((f.code, f.date, f.inst_net, f.foreign_net, f.indiv_net, f.unit, int(f.is_final),
                         f.source, f.asof_date, f.collected_at, f.available_at, rev))
        self.con.executemany("INSERT OR REPLACE INTO investor_flows VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        self.con.commit()
        return len(rows)

    def upsert_universe(self, rows: list[tuple]) -> None:
        self.con.executemany("INSERT OR REPLACE INTO universe(code,name,market,listed_date,delisted_date) VALUES (?,?,?,?,?)", rows)
        self.con.commit()

    def upsert_universe_seen(self, rows: list[tuple], seen_date: str) -> None:
        """날짜별 수집에서 본 종목. listed_date=처음 본 날, delisted_date=마지막으로 본 날(상장폐지 추정용)."""
        for code, name, market, _, _ in rows:
            self.con.execute(
                "INSERT INTO universe(code,name,market,listed_date,delisted_date) VALUES (?,?,?,?,?) "
                "ON CONFLICT(code) DO UPDATE SET name=COALESCE(excluded.name,name), market=excluded.market, "
                "listed_date=MIN(listed_date, excluded.listed_date), delisted_date=MAX(delisted_date, excluded.delisted_date)",
                (code, name, market, seen_date, seen_date))
        self.con.commit()

    def log_collection(self, r: CollectionResult) -> None:
        self.con.execute(
            "INSERT INTO collection_log(source,dataset,date_from,date_to,requested,received,status,message,collected_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (r.source, r.dataset, r.date_from, r.date_to, r.requested, r.received, r.status, r.message, now_iso()))
        self.con.commit()

    def ingest(self, r: CollectionResult) -> None:
        """수집 결과 반영. 실패/부분 수집은 로그에 그대로 남긴다(정상으로 표시하지 않음)."""
        if r.bars:
            self.upsert_bars(r.bars)
        if r.flows:
            self.upsert_flows(r.flows)
        self.log_collection(r)

    # ---- 읽기 ----
    def codes(self) -> list[str]:
        return [r[0] for r in self.con.execute("SELECT DISTINCT code FROM daily_bars ORDER BY code")]

    def dates(self) -> list[str]:
        return [r[0] for r in self.con.execute("SELECT DISTINCT date FROM daily_bars ORDER BY date")]

    def bar(self, code: str, date: str) -> sqlite3.Row | None:
        return self.con.execute("SELECT * FROM daily_bars WHERE code=? AND date=?", (code, date)).fetchone()

    def bars_on(self, date: str) -> dict[str, sqlite3.Row]:
        return {r["code"]: r for r in self.con.execute("SELECT * FROM daily_bars WHERE date=?", (date,))}

    def flows_window(self, end_date: str, n: int, available_before: str | None = None) -> dict[str, list[sqlite3.Row]]:
        """end_date 포함 최근 n 거래일 수급. available_before(시각 문자열) 이전에 확인 가능했던 것만."""
        dates = [r[0] for r in self.con.execute("SELECT DISTINCT date FROM daily_bars WHERE date<=? ORDER BY date DESC LIMIT ?", (end_date, n))]
        if len(dates) < n:
            return {}
        q = "SELECT * FROM investor_flows WHERE date>=? AND date<=?"
        params: list = [dates[-1], end_date]
        if available_before:
            q += " AND available_at<=?"
            params.append(available_before)
        out: dict[str, list] = {}
        for r in self.con.execute(q + " ORDER BY code, date", params):
            out.setdefault(r["code"], []).append(r)
        return out

    def last_collection(self, dataset: str) -> sqlite3.Row | None:
        return self.con.execute("SELECT * FROM collection_log WHERE dataset=? ORDER BY id DESC LIMIT 1", (dataset,)).fetchone()

    def open_issues(self, date: str | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM data_issues WHERE resolved_at IS NULL"
        params: list = []
        if date:
            q += " AND (date=? OR date IS NULL)"
            params.append(date)
        return list(self.con.execute(q + " ORDER BY id", params))

    def fingerprint(self) -> str:
        """같은 데이터인지 확인하기 위한 지문. 시세+수급 전체 행의 해시."""
        h = hashlib.sha256()
        for r in self.con.execute("SELECT code,date,open,high,low,close,volume,value,adj_factor,halted FROM daily_bars ORDER BY code,date"):
            h.update(repr(tuple(r)).encode())
        for r in self.con.execute("SELECT code,date,inst_net,foreign_net,unit,is_final FROM investor_flows ORDER BY code,date"):
            h.update(repr(tuple(r)).encode())
        return h.hexdigest()[:16]
