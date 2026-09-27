"""SQLite 저장소. 시장 데이터, 주문 원장, 운영 상태를 한 파일에 보관해 재시작 복구와 재현을 가능하게 한다."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_bars (
    code TEXT NOT NULL, date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL, value REAL,
    adj_factor REAL NOT NULL DEFAULT 1.0,        -- 누적 수정계수 (원주가 * adj_factor = 수정주가)
    halted INTEGER NOT NULL DEFAULT 0,           -- 거래정지
    open_tradable INTEGER NOT NULL DEFAULT 1,    -- 시가 매매 가능
    close_tradable INTEGER NOT NULL DEFAULT 1,   -- 종가 매매 가능
    source TEXT NOT NULL, asof_date TEXT NOT NULL,
    collected_at TEXT NOT NULL, available_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,         -- 정정 데이터 횟수
    PRIMARY KEY (code, date)
);
CREATE TABLE IF NOT EXISTS investor_flows (
    code TEXT NOT NULL, date TEXT NOT NULL,
    inst_net REAL, foreign_net REAL, indiv_net REAL,
    unit TEXT NOT NULL DEFAULT 'KRW',
    is_final INTEGER NOT NULL DEFAULT 1,         -- 확정치(1) / 잠정치(0)
    source TEXT NOT NULL, asof_date TEXT NOT NULL,
    collected_at TEXT NOT NULL, available_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_bars_date ON daily_bars(date);
CREATE INDEX IF NOT EXISTS idx_flows_date ON investor_flows(date);
CREATE TABLE IF NOT EXISTS corporate_actions (
    code TEXT NOT NULL, date TEXT NOT NULL, kind TEXT NOT NULL,   -- split|merge|dividend|other
    ratio REAL NOT NULL DEFAULT 1.0, note TEXT,
    PRIMARY KEY (code, date, kind)
);
CREATE TABLE IF NOT EXISTS universe (
    code TEXT PRIMARY KEY, name TEXT, market TEXT, listed_date TEXT, delisted_date TEXT
);
CREATE TABLE IF NOT EXISTS collection_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL, dataset TEXT NOT NULL, date_from TEXT, date_to TEXT,
    requested INTEGER, received INTEGER,
    status TEXT NOT NULL,                        -- ok | partial | failed | unsupported
    message TEXT, collected_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS data_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT, date TEXT, dataset TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT,
    blocks_trading INTEGER NOT NULL DEFAULT 1, detected_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    broker_order_id TEXT, run_mode TEXT NOT NULL,
    code TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL,
    order_type TEXT NOT NULL, limit_price REAL,
    state TEXT NOT NULL, filled_qty INTEGER NOT NULL DEFAULT 0, avg_price REAL,
    intent TEXT NOT NULL,                        -- entry | exit
    scheduled_for TEXT NOT NULL,                 -- 예정 시점 (예: 2026-01-05 09:00 open)
    submitted_at TEXT, last_update_at TEXT, filled_at TEXT,
    reason TEXT, error TEXT,
    signal_date TEXT, rank INTEGER
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL, qty INTEGER NOT NULL, price REAL NOT NULL,
    filled_at TEXT NOT NULL, fee REAL NOT NULL DEFAULT 0, tax REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS positions (
    code TEXT PRIMARY KEY, qty INTEGER NOT NULL, avg_price REAL NOT NULL,
    entry_date TEXT NOT NULL, planned_exit_date TEXT NOT NULL,
    entry_order_id TEXT, exit_state TEXT NOT NULL DEFAULT 'holding'   -- holding|exit_pending|exit_blocked
);
CREATE TABLE IF NOT EXISTS closed_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL, qty INTEGER NOT NULL, entry_date TEXT NOT NULL, entry_price REAL NOT NULL,
    exit_date TEXT NOT NULL, exit_price REAL NOT NULL, fees REAL NOT NULL, tax REAL NOT NULL,
    pnl REAL NOT NULL, ret REAL NOT NULL, planned_exit_date TEXT, entry_order_id TEXT, exit_order_id TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS ops_state (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, level TEXT NOT NULL,
    category TEXT NOT NULL, message TEXT NOT NULL, needs_action INTEGER NOT NULL DEFAULT 0,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS candidates (
    date TEXT NOT NULL, code TEXT NOT NULL, rank INTEGER, selected INTEGER NOT NULL,
    score REAL, reasons TEXT NOT NULL, exclusion TEXT, computed_at TEXT NOT NULL,
    PRIMARY KEY (date, code)
);
CREATE TABLE IF NOT EXISTS reconciliations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT NOT NULL,
    code TEXT, local_value TEXT, broker_value TEXT, resolved_at TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS daily_equity (
    date TEXT PRIMARY KEY, cash REAL, market_value REAL, realized_pnl REAL, unrealized_pnl REAL,
    equity REAL, peak REAL, drawdown REAL
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    p = Path(path)
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL") if str(p) != ":memory:" else None
    con.executescript(SCHEMA)
    return con
