"""모의매매 운영 루프. 하루를 세 단계로 나눈다.

  open(d) : 전 거래일 신호 후보를 시가 매수 (주문 전 미해결 주문 해소·잔고 대조·위험 판단)
  close(d): 예정 청산일 도래 보유분을 종가 매도, 일별 평가 기록
  eod(d)  : 데이터 품질 검사, 다음 거래일 후보 산출·저장

모든 상태는 SQLite에 있으므로 재시작 후 같은 날짜·단계를 다시 실행해도 중복 주문이 나지 않는다
(client_order_id가 결정적이고, 단계 완료 여부가 ops_state에 기록된다).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from ..calendar import TradingCalendar
from ..config import Settings, RunMode
from ..data.models import now_iso
from ..data.quality import run_quality_checks, blocked_codes
from ..data.store import MarketStore
from ..strategy.signal import compute_candidates, persist_candidates
from ..broker.base import Broker, Order, OrderState, tick_floor
from ..broker.ledger import OrderLedger, make_client_order_id
from ..risk.limits import RiskManager


@dataclass
class PhaseReport:
    date: str
    phase: str
    actions: list[str]
    blocked: list[str]


class Runner:
    def __init__(self, settings: Settings, con: sqlite3.Connection, broker: Broker, cal: TradingCalendar | None = None):
        if settings.mode == RunMode.REAL:
            raise RuntimeError("실계좌 차단")
        self.s = settings
        self.con = con
        self.store = MarketStore(con)
        self.broker = broker
        self.cal = cal or TradingCalendar()
        self.ledger = OrderLedger(con, settings.mode.value)
        self.risk = RiskManager(con, settings.risk)

    # ---- 상태 ----
    def _state(self, key: str, default=None):
        r = self.con.execute("SELECT value FROM ops_state WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def _set_state(self, key: str, value) -> None:
        self.con.execute("INSERT OR REPLACE INTO ops_state(key,value,updated_at) VALUES (?,?,?)", (key, json.dumps(value, ensure_ascii=False), now_iso()))
        self.con.commit()

    def event(self, level: str, category: str, message: str, needs_action: bool = False) -> None:
        self.con.execute("INSERT INTO events(at,level,category,message,needs_action) VALUES (?,?,?,?,?)", (now_iso(), level, category, message, int(needs_action)))
        self.con.commit()

    def positions(self) -> list[sqlite3.Row]:
        return list(self.con.execute("SELECT * FROM positions ORDER BY entry_date"))

    def realized_pnl(self) -> float:
        return self.con.execute("SELECT COALESCE(SUM(pnl),0) FROM closed_positions").fetchone()[0]

    # ---- 복구·대조 ----
    def recover(self) -> list[str]:
        """재시작 후: 미해결 주문을 브로커 조회로 갱신하고 체결분을 포지션에 반영, 잔고 대조."""
        notes = []
        for o in self.ledger.open_orders():
            before = o.state
            o = self.ledger.resolve(self.broker, o)
            if o.state != before:
                notes.append(f"{o.code} {o.side} {before.value}->{o.state.value}")
            self._settle(o)
            if o.state == OrderState.UNKNOWN:
                self.event("error", "order", f"{o.code} {o.side} 주문 결과 불명(id {o.client_order_id[:8]}) — 수동 확인 필요", needs_action=True)
        notes += self.reconcile()
        return notes

    def reconcile(self) -> list[str]:
        """프로그램 포지션 vs 증권사 잔고. 불일치는 reconciliations에 기록되고 신규매수를 막는다."""
        acct = self.broker.account()
        local = {r["code"]: r["qty"] for r in self.positions()}
        # 아직 열린 주문의 체결분은 포지션에 반영 전이므로 기대 수량에 더한다(매수 +, 매도 -)
        for o in self.ledger.open_orders():
            if o.filled_qty:
                local[o.code] = local.get(o.code, 0) + (o.filled_qty if o.side == "buy" else -o.filled_qty)
        notes = []
        for code in set(local) | set(acct.positions):
            lq, bq = local.get(code, 0), acct.positions.get(code, (0, 0))[0]
            if lq != bq:
                exists = self.con.execute("SELECT 1 FROM reconciliations WHERE code=? AND resolved_at IS NULL AND kind='position'", (code,)).fetchone()
                if not exists:
                    self.con.execute("INSERT INTO reconciliations(at,kind,code,local_value,broker_value) VALUES (?,?,?,?,?)", (now_iso(), "position", code, str(lq), str(bq)))
                    self.con.commit()
                    self.event("error", "reconcile", f"{code} 수량 불일치 로컬 {lq} vs 증권사 {bq}", needs_action=True)
                notes.append(f"불일치 {code}: {lq} vs {bq}")
        self._set_state("last_account", {"cash": acct.cash, "orderable": acct.orderable_cash, "asof": acct.asof, "verified_live": acct.verified_live})
        return notes

    def unresolved_mismatches(self) -> int:
        return self.con.execute("SELECT COUNT(*) FROM reconciliations WHERE resolved_at IS NULL").fetchone()[0]

    def resolve_mismatch(self, rec_id: int, note: str) -> None:
        self.con.execute("UPDATE reconciliations SET resolved_at=?, note=? WHERE id=?", (now_iso(), note, rec_id))
        self.con.commit()

    # ---- 체결 반영 ----
    def _settle(self, o: Order) -> None:
        """전량체결, 또는 부분체결 후 취소된 주문의 체결분을 포지션에 반영한다."""
        if o.state == OrderState.FILLED or (o.state == OrderState.CANCELLED and o.filled_qty > 0):
            self._apply_fill(o)

    def _apply_fill(self, o: Order) -> None:
        if o.intent == "entry":
            if self.con.execute("SELECT 1 FROM positions WHERE code=?", (o.code,)).fetchone():
                return
            date = o.scheduled_for.split(" ")[0]
            exit_date = self.cal.nth_trading_day_from(_d(date), self.s.strategy.hold_days).isoformat()
            self.con.execute("INSERT INTO positions(code,qty,avg_price,entry_date,planned_exit_date,entry_order_id) VALUES (?,?,?,?,?,?)",
                             (o.code, o.filled_qty, o.avg_price, date, exit_date, o.client_order_id))
        else:
            p = self.con.execute("SELECT * FROM positions WHERE code=?", (o.code,)).fetchone()
            if p is None or self.con.execute("SELECT 1 FROM closed_positions WHERE exit_order_id=?", (o.client_order_id,)).fetchone():
                return
            date = o.scheduled_for.split(" ")[0]
            c = self.s.costs
            fees = (p["avg_price"] * o.filled_qty + o.avg_price * o.filled_qty) * c.commission_rate
            tax = o.avg_price * o.filled_qty * c.sell_tax_rate
            pnl = (o.avg_price - p["avg_price"]) * o.filled_qty - fees - tax
            note = "" if date == p["planned_exit_date"] else f"예정일 {p['planned_exit_date']} 대신 {date} 청산"
            self.con.execute("INSERT INTO closed_positions(code,qty,entry_date,entry_price,exit_date,exit_price,fees,tax,pnl,ret,planned_exit_date,entry_order_id,exit_order_id,note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (o.code, o.filled_qty, p["entry_date"], p["avg_price"], date, o.avg_price, fees, tax, pnl, pnl / (p["avg_price"] * o.filled_qty), p["planned_exit_date"], p["entry_order_id"], o.client_order_id, note))
            if o.filled_qty >= p["qty"]:
                self.con.execute("DELETE FROM positions WHERE code=?", (o.code,))
            else:
                self.con.execute("UPDATE positions SET qty=qty-?, exit_state='holding' WHERE code=?", (o.filled_qty, o.code))
        self.con.commit()

    def _place(self, o: Order) -> Order:
        o = self.ledger.submit(self.broker, o)
        if o.state in (OrderState.SUBMITTED, OrderState.UNFILLED, OrderState.PARTIAL):
            o = self.ledger.resolve(self.broker, o)   # 시장가는 곧바로 체결되는 경우가 많음
        self._settle(o)
        if o.state == OrderState.UNKNOWN:
            self.event("error", "order", f"{o.code} {o.side} 결과 불명 — 다음 주기 조회로 해결 전 신규매수 차단", needs_action=True)
        elif o.state == OrderState.REJECTED:
            self.event("warn", "order", f"{o.code} {o.side} 거절: {o.error}")
        return o

    # ---- 단계 ----
    def _done(self, date: str, phase: str) -> bool:
        return (self._state("phases_done") or {}).get(f"{date}:{phase}", False)

    def _mark_done(self, date: str, phase: str) -> None:
        d = self._state("phases_done") or {}
        d[f"{date}:{phase}"] = True
        self._set_state("phases_done", d)

    def run_open(self, date: str) -> PhaseReport:
        rep = PhaseReport(date, "open", [], [])
        if self._done(date, "open"):
            rep.blocked.append("이미 실행된 단계(재실행 방지)")
            return rep
        rep.actions += self.recover()
        sig = self.cal.prev_trading_day(_d(date)).isoformat()
        rows = list(self.con.execute("SELECT * FROM candidates WHERE date=? AND selected=1 ORDER BY rank", (sig,)))
        if not rows:
            rep.blocked.append(f"신호일 {sig} 후보 없음(eod 미실행 또는 조건 충족 종목 없음)")
        acct = self._state("last_account") or {}
        pending_buys = [o for o in self.ledger.open_orders() if o.side == "buy"]
        pending_amt = sum((o.limit_price or 0) * o.qty for o in pending_buys)
        daily_bought = self.con.execute("SELECT COALESCE(SUM(COALESCE(avg_price,limit_price,0)*qty),0) FROM orders WHERE run_mode=? AND side='buy' AND scheduled_for LIKE ? AND state NOT IN ('rejected','cancelled')",
                                        (self.s.mode.value, f"{date} %")).fetchone()[0]
        blocked = blocked_codes(self.con, date)
        decision = self.risk.evaluate(self.realized_pnl(), len(self.positions()), len(pending_buys), len(self.ledger.unknown_orders()),
                                      self.unresolved_mismatches(), blocked, daily_bought)
        if not decision.allow_new_buys:
            rep.blocked += decision.reasons
            self._mark_done(date, "open")
            return rep
        held = {r["code"] for r in self.positions()}
        n_slots = self.s.risk.max_positions - len(held) - len(pending_buys)
        for r in rows:
            if n_slots <= 0:
                rep.blocked.append(f"{r['code']} 보유 한도 도달")
                continue
            if r["code"] in held or self.ledger.open_orders(r["code"]):
                rep.blocked.append(f"{r['code']} 이미 보유/주문 중")
                continue
            if r["code"] in blocked:
                rep.blocked.append(f"{r['code']} 데이터 이슈: {blocked[r['code']]}")
                continue
            try:
                q = self.broker.quote(r["code"], date)
            except Exception as e:
                rep.blocked.append(f"{r['code']} 시세 조회 실패: {e}")
                continue
            if q.halted or not q.ref_price:
                rep.blocked.append(f"{r['code']} 거래정지 또는 기준가 없음")
                continue
            # 지정가 = 기준가(신호일 종가) x (1+한도). 장전 동시호가에서 시가가 이 이하이면 시가에 체결된다.
            limit_px = tick_floor(q.ref_price * (1 + self.s.strategy.entry_limit_buffer))
            qty, why = self.risk.buy_qty(limit_px, acct.get("orderable", 0), pending_amt, daily_bought, self.s.costs.commission_rate)
            if why:
                rep.blocked.append(f"{r['code']} {why}")
                continue
            o = Order(make_client_order_id(self.s.mode.value, r["code"], "buy", "entry", f"{date} open"), r["code"], "buy", qty, "limit", "entry",
                      f"{date} open", limit_price=limit_px, reason=f"rank {r['rank']}: " + "; ".join(json.loads(r["reasons"])), signal_date=sig, rank=r["rank"])
            o = self._place(o)
            rep.actions.append(f"BUY {o.code} x{qty} -> {o.state.value}" + (f" @{o.avg_price:,.0f}" if o.avg_price else ""))
            if o.state not in (OrderState.REJECTED, OrderState.CANCELLED):
                n_slots -= 1
                daily_bought += limit_px * qty
                pending_amt += limit_px * qty if o.state != OrderState.FILLED else 0
                held.add(o.code)
            if o.state == OrderState.UNKNOWN:
                rep.blocked.append("결과 불명 주문 발생 — 이후 신규매수 중단")
                break
        self._mark_done(date, "open")
        return rep

    def run_close(self, date: str) -> PhaseReport:
        rep = PhaseReport(date, "close", [], [])
        if self._done(date, "close"):
            rep.blocked.append("이미 실행된 단계(재실행 방지)")
            return rep
        rep.actions += self.recover()
        acct_pos = self.broker.account()
        for p in self.positions():
            if p["planned_exit_date"] > date:
                continue
            code = p["code"]
            if self.ledger.open_orders(code):
                rep.blocked.append(f"{code} 미해결 주문 있어 청산 보류")
                continue
            # 청산은 데이터 이슈로 막지 않는다(신규 매수만 차단). 거래정지일 때만 다음 거래일로 미룬다.
            try:
                q = self.broker.quote(code, date)
                why = "거래정지" if q.halted else None
            except Exception as e:
                why = f"시세 조회 실패: {e}"
            if why:
                self.con.execute("UPDATE positions SET exit_state='exit_blocked' WHERE code=?", (code,))
                self.con.commit()
                self.event("warn", "exit", f"{code} 예정 청산일 {p['planned_exit_date']} 종가 매도 불가({why}) — 다음 거래일 재시도", needs_action=True)
                rep.blocked.append(f"{code} 청산 불가: {why}")
                continue
            sellable = acct_pos.sellable.get(code, 0)
            qty = min(p["qty"], sellable) if acct_pos.verified_live or self.broker.name == "paper" else p["qty"]
            if qty <= 0:
                rep.blocked.append(f"{code} 매도가능수량 0")
                self.event("error", "exit", f"{code} 매도가능수량 0 (보유 {p['qty']})", needs_action=True)
                continue
            o = Order(make_client_order_id(self.s.mode.value, code, "sell", "exit", f"{date} close"), code, "sell", qty, "market", "exit",
                      f"{date} close", reason=f"보유 {self.s.strategy.hold_days}거래일 종가 청산")
            self.con.execute("UPDATE positions SET exit_state='exit_pending' WHERE code=?", (code,))
            self.con.commit()
            o = self._place(o)
            if o.state == OrderState.REJECTED:
                self.con.execute("UPDATE positions SET exit_state='exit_blocked' WHERE code=?", (code,))
                self.con.commit()
            rep.actions.append(f"SELL {o.code} x{qty} -> {o.state.value}" + (f" @{o.avg_price:,.0f}" if o.avg_price else ""))
        self._mark_done(date, "close")
        return rep

    def run_sync(self, date: str, phase: str) -> PhaseReport:
        """동시호가 체결 뒤 실행. 체결을 조회해 반영하고, 남은 미체결은 취소한다.
        phase='open': 시가 미체결 매수(갭 상승 등)를 취소. phase='close': 종가 미체결 매도를 취소하고 다음 거래일 재시도로 표시."""
        name = f"sync_{phase}"
        rep = PhaseReport(date, name, [], [])
        if self._done(date, name):
            rep.blocked.append("이미 실행된 단계(재실행 방지)")
            return rep
        rep.actions += self.recover()
        intent = "entry" if phase == "open" else "exit"
        for o in self.ledger.open_orders():
            if o.intent != intent or not o.scheduled_for.startswith(date) or o.state == OrderState.UNKNOWN:
                continue
            try:
                self.ledger.apply(o, self.broker.cancel(o))
                o = self.ledger.resolve(self.broker, o)
            except Exception as e:
                rep.blocked.append(f"{o.code} 취소 실패: {e}")
                self.event("error", "order", f"{o.code} 미체결 취소 실패: {e}", needs_action=True)
                continue
            self._settle(o)
            what = "시가 미체결 매수 취소(지정가 초과 갭 또는 미체결)" if intent == "entry" else "종가 미체결 매도 취소 — 다음 거래일 재시도"
            rep.actions.append(f"{o.code} {what} -> {o.state.value}, 체결 {o.filled_qty}/{o.qty}")
            self.event("warn", "order", f"{o.code} {what}", needs_action=(intent == "exit"))
            if intent == "exit":
                self.con.execute("UPDATE positions SET exit_state='exit_blocked' WHERE code=?", (o.code,))
                self.con.commit()
        self.reconcile()
        self._mark_done(date, name)
        return rep

    def run_eod(self, date: str) -> PhaseReport:
        rep = PhaseReport(date, "eod", [], [])
        if not self.store.bars_on(date):
            rep.blocked.append(f"{date} 시세 데이터 없음 — 수집 필요. 후보 산출 생략")
            self.event("error", "data", f"{date} 시세/수급 미수집 — 다음 거래일 매수 불가", needs_action=True)
            return rep
        new = run_quality_checks(self.con, [date])
        if new:
            rep.actions.append(f"데이터 이슈 {len(new)}건 발견")
        nxt = self.cal.next_trading_day(_d(date)).isoformat()
        cands = compute_candidates(self.store, date, self.s.strategy, f"{nxt} {self.s.decision_time}:00", blocked_codes(self.con, date))
        persist_candidates(self.store, cands, now_iso())
        rep.actions.append(f"후보 {sum(c.selected for c in cands)} 선정 / {sum(not c.selected for c in cands)} 제외")
        self._snapshot_equity(date)
        self._set_state("last_eod", {"date": date, "at": now_iso()})
        self._mark_done(date, "eod")
        return rep

    def _snapshot_equity(self, date: str) -> None:
        acct = self._state("last_account") or {}
        cash = acct.get("cash", 0.0)
        mv = unreal = 0.0
        for p in self.positions():
            b = self.store.bar(p["code"], date)
            px = b["close"] if b and b["close"] else p["avg_price"]
            mv += px * p["qty"]
            unreal += (px - p["avg_price"]) * p["qty"]
        eq = cash + mv
        prev = self.con.execute("SELECT peak FROM daily_equity WHERE date<? ORDER BY date DESC LIMIT 1", (date,)).fetchone()
        peak = max(prev[0] if prev else eq, eq)
        self.con.execute("INSERT OR REPLACE INTO daily_equity VALUES (?,?,?,?,?,?,?,?)", (date, cash, mv, self.realized_pnl(), unreal, eq, peak, eq / peak - 1 if peak else 0))
        self.con.commit()

    def simulate(self, start: str, end: str) -> list[PhaseReport]:
        """저장된 데이터로 날짜별 open→close→eod를 순서대로 실행 (페이퍼 브로커 전용)."""
        reps = []
        for d in [x for x in self.store.dates() if start <= x <= end]:
            reps += [self.run_open(d), self.run_sync(d, "open"), self.run_close(d), self.run_sync(d, "close"), self.run_eod(d)]
        return reps


def _d(s: str):
    from datetime import date
    return date.fromisoformat(s)
