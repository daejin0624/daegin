"""로컬 운영 화면. 표준 라이브러리 http.server만 사용한다. 인증정보는 어떤 응답에도 포함되지 않는다."""
from __future__ import annotations

import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..config import Settings, RunMode
from ..data.models import now_iso
from ..risk.limits import RiskManager

HTML = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")


def status_payload(con: sqlite3.Connection, settings: Settings, results_dir: str) -> dict:
    def rows(q, *p):
        return [dict(r) for r in con.execute(q, p)]
    def state(k, default=None):
        r = con.execute("SELECT value, updated_at FROM ops_state WHERE key=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default
    risk = RiskManager(con, settings.risk)
    halt = risk.halt_state().__dict__
    acct = state("last_account") or {}
    last_eq = con.execute("SELECT * FROM daily_equity ORDER BY date DESC LIMIT 1").fetchone()
    positions = rows("SELECT * FROM positions ORDER BY entry_date")
    last_date = con.execute("SELECT MAX(date) FROM daily_bars").fetchone()[0]
    for p in positions:
        b = con.execute("SELECT close FROM daily_bars WHERE code=? AND date=?", (p["code"], last_date)).fetchone() if last_date else None
        px = b[0] if b and b[0] else p["avg_price"]
        p["last_price"] = px
        p["market_value"] = px * p["qty"]
        p["unrealized"] = (px - p["avg_price"]) * p["qty"]
        p["hold_days"] = con.execute("SELECT COUNT(DISTINCT date) FROM daily_bars WHERE date>=? AND date<=?", (p["entry_date"], last_date or p["entry_date"])).fetchone()[0]
    last_cand_date = con.execute("SELECT MAX(date) FROM candidates").fetchone()[0]
    cands = rows("SELECT * FROM candidates WHERE date=? ORDER BY selected DESC, rank", last_cand_date) if last_cand_date else []
    for c in cands:
        c["reasons"] = json.loads(c["reasons"])
    coll = rows("SELECT * FROM collection_log ORDER BY id DESC LIMIT 5")
    results = []
    rd = Path(results_dir)
    if rd.exists():
        for d in sorted(rd.iterdir(), reverse=True)[:30]:
            sp = d / "summary.json"
            if sp.exists():
                try:
                    m = json.loads(sp.read_text(encoding="utf-8"))
                    results.append({"name": d.name, "data_kind": m.get("data_kind"), "period": m.get("period"), "summary": m.get("summary"), "warnings": m.get("warnings")})
                except Exception:
                    pass
            elif d.suffix == ".json":
                results.append({"name": d.name, "data_kind": "comparison", "period": None, "summary": None})
    realized = con.execute("SELECT COALESCE(SUM(pnl),0) FROM closed_positions").fetchone()[0]
    return {
        "now": now_iso(),
        "mode": settings.mode.value,
        "mode_label": {"synthetic": "합성 데이터 테스트", "backtest": "실제 데이터 백테스트", "paper": "내장 페이퍼 모의매매", "kis_vts": "한국투자증권 모의투자(미검증)"}.get(settings.mode.value, settings.mode.value),
        "real_account_blocked": True,
        "halt": halt,
        "risk_limits": settings.risk.__dict__,
        "strategy": settings.strategy.__dict__,
        "data": {"last_date": last_date, "last_eod": state("last_eod"), "collections": coll,
                 "open_issues": rows("SELECT * FROM data_issues WHERE resolved_at IS NULL ORDER BY id DESC LIMIT 50"),
                 "open_issue_count": con.execute("SELECT COUNT(*) FROM data_issues WHERE resolved_at IS NULL AND blocks_trading=1").fetchone()[0]},
        "account": {**acct, "realized_pnl": realized, "unrealized_pnl": sum(p["unrealized"] for p in positions),
                    "market_value": sum(p["market_value"] for p in positions),
                    "equity": (acct.get("cash", 0) + sum(p["market_value"] for p in positions)),
                    "drawdown": last_eq["drawdown"] if last_eq else 0.0, "equity_date": last_eq["date"] if last_eq else None},
        "positions": positions,
        "candidates": {"date": last_cand_date, "items": cands},
        "orders": rows("SELECT * FROM orders WHERE run_mode=? ORDER BY COALESCE(submitted_at, scheduled_for) DESC LIMIT 50", settings.mode.value),
        "open_orders": rows("SELECT * FROM orders WHERE run_mode=? AND state NOT IN ('filled','rejected','cancelled')", settings.mode.value),
        "unknown_orders": rows("SELECT * FROM orders WHERE run_mode=? AND state='unknown'", settings.mode.value),
        "reconciliations": rows("SELECT * FROM reconciliations WHERE resolved_at IS NULL"),
        "closed": rows("SELECT * FROM closed_positions ORDER BY id DESC LIMIT 50"),
        "events": rows("SELECT * FROM events ORDER BY id DESC LIMIT 50"),
        "needs_action": rows("SELECT * FROM events WHERE needs_action=1 AND resolved_at IS NULL ORDER BY id DESC"),
        "equity_curve": rows("SELECT date, equity, drawdown FROM daily_equity ORDER BY date"),
        "results": results,
        "broker_features": state("broker_features") or {},
    }


def make_handler(con: sqlite3.Connection, settings: Settings, results_dir: str):
    lock = threading.Lock()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # 조용히
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                body = HTML.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/status":
                with lock:
                    self._json(status_payload(con, settings, results_dir))
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n) or b"{}") if n else {}
            risk = RiskManager(con, settings.risk)
            with lock:
                if self.path == "/api/halt":
                    st = risk.set_halt(True, data.get("reason") or "사용자 수동 중단", "manual")
                    con.execute("INSERT INTO events(at,level,category,message) VALUES (?,?,?,?)", (now_iso(), "warn", "control", f"수동 중단: {st.reason}"))
                    con.commit()
                    self._json(st.__dict__)
                elif self.path == "/api/resume":
                    st = risk.resume()
                    con.execute("INSERT INTO events(at,level,category,message) VALUES (?,?,?,?)", (now_iso(), "info", "control", "재개" if not st.halted else "재개 불가"))
                    con.commit()
                    self._json(st.__dict__)
                elif self.path == "/api/resolve_event":
                    con.execute("UPDATE events SET resolved_at=? WHERE id=?", (now_iso(), int(data["id"])))
                    con.commit()
                    self._json({"ok": True})
                elif self.path == "/api/resolve_reconciliation":
                    con.execute("UPDATE reconciliations SET resolved_at=?, note=? WHERE id=?", (now_iso(), data.get("note", "수동 확인"), int(data["id"])))
                    con.commit()
                    self._json({"ok": True})
                else:
                    self._json({"error": "not found"}, 404)

    return H


def serve(con: sqlite3.Connection, settings: Settings, results_dir: str, port: int | None = None) -> None:
    port = port or settings.dashboard_port
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(con, settings, results_dir))
    print(f"대시보드: http://127.0.0.1:{port}  (모드: {settings.mode.value}, 실계좌 차단)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
