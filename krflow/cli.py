"""명령행 진입점: python -m krflow.cli <command> ..."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from . import ENGINE_VERSION
from .calendar import TradingCalendar
from .config import Settings, RunMode, RealAccountBlocked, mask, kis_credentials
from .db import connect
from .data.store import MarketStore
from .data.quality import run_quality_checks
from .data import synthetic, csv_source, pykrx_source
from .backtest.engine import run_backtest
from .backtest.metrics import summarize, benchmark_return
from .backtest.compare import compare_strategies, cost_sensitivity, train_test
from .backtest.report import save_backtest, save_json


def _settings(args) -> Settings:
    s = Settings.load(getattr(args, "config", None))
    if getattr(args, "mode", None):
        s = Settings(**{**s.to_dict(), "mode": args.mode})
    if getattr(args, "db", None):
        s.db_path = args.db
    return s


def _print_summary(m: dict, title: str) -> None:
    print(f"\n== {title} ==")
    for k in ("start", "end", "initial_cash", "final_equity", "total_return", "max_drawdown", "trades", "closed_trades", "open_positions", "exit_blocked", "win_rate", "avg_trade_return", "realized_pnl", "unrealized_pnl", "total_fees", "total_tax", "skipped", "data_fingerprint"):
        v = m.get(k)
        if isinstance(v, float) and k in ("total_return", "max_drawdown", "win_rate", "avg_trade_return"):
            v = f"{v:.2%}"
        elif isinstance(v, float):
            v = f"{v:,.0f}"
        print(f"  {k:18s} {v}")


def cmd_synth(a):
    s = _settings(a)
    con = connect(s.db_path)
    r = synthetic.generate(date.fromisoformat(a.start), date.fromisoformat(a.end), a.codes, a.seed, a.flow_effect, TradingCalendar.load(a.calendar), a.inject_issues)
    MarketStore(con).ingest(r)
    issues = run_quality_checks(con)
    print(f"합성 데이터 {r.received}행 저장 ({r.date_from}~{r.date_to}, seed={a.seed}, flow_effect={a.flow_effect}); 품질 이슈 {len(issues)}건")
    print("주의: 합성 데이터 결과는 실제 투자 성과가 아니다.")


def cmd_import_csv(a):
    s = _settings(a)
    con = connect(s.db_path)
    r = csv_source.load(a.bars, a.flows)
    MarketStore(con).ingest(r)
    print(f"[{r.status}] {r.message}; bars {len(r.bars)} flows {len(r.flows)}")
    if r.status != "failed":
        print(f"품질 이슈 {len(run_quality_checks(con))}건")


def cmd_collect_pykrx(a):
    import os
    s = _settings(a)
    con = connect(s.db_path)
    if not (os.environ.get("KRX_ID") and os.environ.get("KRX_PW")):
        print("경고: KRX_ID / KRX_PW 환경변수가 없습니다. pykrx 1.2.x는 KRX 로그인이 필요하며, 없으면 빈 결과(수집 실패)가 기록됩니다.")
    cal = TradingCalendar.load(a.calendar)
    summary = pykrx_source.collect_range(MarketStore(con), date.fromisoformat(a.start), date.fromisoformat(a.end), cal, pause=a.pause)
    print(f"수집 요약: {summary} (어댑터 검증 상태: {'검증' if pykrx_source.VERIFIED else '미검증'})")
    if summary["failed"] or summary["partial"]:
        print("실패/부분 수집일이 있습니다. collection_log 또는 대시보드에서 확인 후 같은 명령으로 다시 실행하면 빠진 날만 받습니다.")
    new = run_quality_checks(con)
    print(f"품질 이슈 {len(new)}건")


def cmd_quality(a):
    s = _settings(a)
    con = connect(s.db_path)
    new = run_quality_checks(con)
    print(f"새 이슈 {len(new)}건")
    for r in con.execute("SELECT kind, COUNT(*) n, SUM(blocks_trading) b FROM data_issues WHERE resolved_at IS NULL GROUP BY kind"):
        print(f"  {r['kind']:18s} {r['n']:5d}건 (차단 {r['b']})")


def _kind(a, con) -> str:
    src = con.execute("SELECT DISTINCT source FROM daily_bars").fetchall()
    srcs = {r[0] for r in src}
    if a.kind:
        return a.kind
    return "synthetic" if srcs == {"synthetic"} else "real_backtest"


def cmd_backtest(a):
    s = _settings(a)
    con = connect(s.db_path)
    store = MarketStore(con)
    kind = _kind(a, con)
    r = run_backtest(store, s, a.start, a.end)
    m = summarize(r)
    _print_summary(m, f"백테스트 [{kind}] {a.start}~{a.end}")
    bm = benchmark_return(store, a.start, a.end, a.benchmark)
    print(f"  benchmark          {bm['kind']}: {bm['return']:.2%}" if bm.get("return") is not None else f"  benchmark          {bm}")
    out = save_backtest(r, a.out, "backtest", kind)
    print(f"저장: {out}")
    for w in r.warnings:
        print(f"  ! {w}")


def cmd_compare(a):
    s = _settings(a)
    con = connect(s.db_path)
    store = MarketStore(con)
    kind = _kind(a, con)
    res = {"data_kind": kind, "engine_version": ENGINE_VERSION, "settings": s.to_dict(), "data_fingerprint": store.fingerprint()}
    if a.split:
        tt = train_test(store, s, a.start, a.end, a.split)
        res["train_test"] = tt
        for part in ("train", "test"):
            print(f"\n== {part} {tt[part]['start']}~{tt[part]['end']} ==")
            for name, m in tt[part].items():
                if isinstance(m, dict) and "total_return" in m:
                    print(f"  {name:28s} 수익 {m['total_return']:+.2%}  MDD {m['max_drawdown']:.2%}  거래 {m['trades']:3d}  승률 {(m['win_rate'] or 0):.1%}")
            print(f"  효과: {tt[part]['_effects']}  벤치마크: {tt[part]['_benchmark']}")
    else:
        cmp_ = compare_strategies(store, s, a.start, a.end)
        res["compare"] = cmp_
        print(f"\n== 전략 비교 [{kind}] {a.start}~{a.end} ==")
        for name, m in cmp_.items():
            if isinstance(m, dict) and "total_return" in m:
                print(f"  {name:28s} 수익 {m['total_return']:+.2%}  MDD {m['max_drawdown']:.2%}  거래 {m['trades']:3d}  승률 {(m['win_rate'] or 0):.1%}")
        print(f"  효과: {cmp_['_effects']}\n  벤치마크: {cmp_['_benchmark']}")
    cs = cost_sensitivity(store, s, a.start, a.end)
    res["cost_sensitivity"] = cs
    print("\n== 비용 민감도 ==")
    for row in cs:
        print(f"  {row['scenario']:30s} 수익 {row['total_return']:+.2%}  MDD {row['max_drawdown']:.2%}  거래당 평균 {(row['avg_trade_return'] or 0):+.3%}")
    p = save_json(res, a.out, f"compare_{kind}")
    print(f"저장: {p}")


def _make_runner(s: Settings, con, a):
    from .ops.runner import Runner
    if s.mode == RunMode.KIS_VTS:
        from .broker.kis import KISBroker
        broker = KISBroker()
    else:
        from .broker.paper import PaperBroker
        broker = PaperBroker(MarketStore(con), s.costs, s.risk.initial_cash)
    r = Runner(s, con, broker, TradingCalendar.load(a.calendar))
    features = broker.feature_status()
    for k, v in (r._state("kis_verification") or {}).items():
        if k in features and s.mode == RunMode.KIS_VTS:
            features[k] = v
    r._set_state("broker_features", features)
    return r



def cmd_paper(a):
    s = _settings(a)
    if s.mode not in (RunMode.PAPER, RunMode.SYNTHETIC):
        s = Settings(**{**s.to_dict(), "mode": "paper"})
    con = connect(s.db_path)
    r = _make_runner(s, con, a)
    reps = r.simulate(a.start, a.end)
    for rep in reps:
        if rep.actions or (a.verbose and rep.blocked):
            print(f"{rep.date} {rep.phase:5s} " + "; ".join(rep.actions) + ("  [차단: " + "; ".join(rep.blocked) + "]" if rep.blocked and a.verbose else ""))
    eq = con.execute("SELECT * FROM daily_equity ORDER BY date DESC LIMIT 1").fetchone()
    n_closed = con.execute("SELECT COUNT(*) FROM closed_positions").fetchone()[0]
    print(f"\n페이퍼 모의매매 완료: 평가자산 {eq['equity']:,.0f} (실현 {eq['realized_pnl']:,.0f}, 미실현 {eq['unrealized_pnl']:,.0f}, 낙폭 {eq['drawdown']:.2%}), 청산 {n_closed}건")
    print(f"대시보드에서 확인: python -m krflow.cli dashboard --db {s.db_path}")


def cmd_phase(a):
    s = _settings(a)
    con = connect(s.db_path)
    r = _make_runner(s, con, a)
    d = a.date or date.today().isoformat()
    rep = {"open": r.run_open, "close": r.run_close, "eod": r.run_eod,
           "sync_open": lambda d: r.run_sync(d, "open"), "sync_close": lambda d: r.run_sync(d, "close")}[a.phase](d)
    print(json.dumps(rep.__dict__, ensure_ascii=False, indent=1))


SCHEDULE = (   # (단계, 시각). 장전 동시호가 08:30~09:00, 종가 동시호가 15:20~15:30
    ("open", "08:50"),        # 지정가 매수를 장전 동시호가에 제출 -> 09:00 시가로 체결
    ("sync_open", "09:05"),   # 체결 반영, 미체결(갭 상승) 매수 취소
    ("close", "15:21"),       # 시장가 매도를 종가 동시호가에 제출 -> 15:30 종가로 체결
    ("sync_close", "15:40"),  # 체결 반영, 미체결 매도 취소 -> 다음 거래일 재시도
    ("collect", "18:20"),     # 당일 시세·수급 수집 (pykrx, KRX 로그인 필요)
    ("eod", "18:30"),         # 품질검사, 다음 거래일 후보 산출, 일별 평가
)


def cmd_serve(a):
    """모의투자 스케줄러 [미검증]. 휴장일 달력(--calendar)을 반드시 지정할 것."""
    import time
    from datetime import datetime
    s = _settings(a)
    con = connect(s.db_path)
    r = _make_runner(s, con, a)
    if not Path(a.calendar).exists():
        print(f"경고: 휴장일 파일 {a.calendar} 없음. 설·추석·선거일 등 휴장일을 모르면 휴장일에 주문을 시도합니다(증권사에서 거절됨).")
    print(f"스케줄러 시작 (모드 {s.mode.value}, 실계좌 차단). Ctrl+C로 종료")
    print("일정: " + ", ".join(f"{p} {t}" for p, t in SCHEDULE))
    while True:
        now = datetime.now()
        d = now.date()
        ds = d.isoformat()
        if r.cal.is_trading_day(d):
            hm = now.strftime("%H:%M")
            for phase, t in SCHEDULE:
                if hm < t or r._done(ds, phase):
                    continue
                try:
                    if phase == "collect":
                        res, uni = pykrx_source.fetch_day(d)
                        r.store.ingest(res)
                        if uni:
                            r.store.upsert_universe_seen(uni, ds)
                        if res.status != "ok":
                            r.event("error", "data", f"{ds} 수집 {res.status}: {res.message}", needs_action=True)
                        r._mark_done(ds, phase)
                        print(f"{now:%H:%M} collect [{res.status}] {res.message}")
                        continue
                    rep = r.run_sync(ds, phase[5:]) if phase.startswith("sync_") else getattr(r, f"run_{phase}")(ds)
                    print(f"{now:%H:%M} {phase}: {rep.actions} {rep.blocked}")
                except Exception as e:   # 한 단계 실패가 스케줄러 전체를 멈추지 않게 하되, 조치 필요로 남긴다
                    r.event("error", "scheduler", f"{phase} 실패: {type(e).__name__}: {e}", needs_action=True)
                    print(f"{now:%H:%M} {phase} 실패: {type(e).__name__}: {e}")
                    r._mark_done(ds, phase)
        time.sleep(20)


def cmd_dashboard(a):
    from .ui.server import serve
    s = _settings(a)
    con = connect(s.db_path)
    serve(con, s, a.out, a.port)


def cmd_halt(a):
    from .risk.limits import RiskManager
    s = _settings(a)
    con = connect(s.db_path)
    st = RiskManager(con, s.risk).set_halt(True, a.reason, "manual")
    print(st)


def cmd_resume(a):
    from .risk.limits import RiskManager
    s = _settings(a)
    con = connect(s.db_path)
    print(RiskManager(con, s.risk).resume())


def cmd_kis_verify(a):
    """한투 모의투자 연동을 단계별로 실제 호출해 검증하고 결과를 DB에 기록한다.
    주문 검증은 삼성전자 1주를 하한가 지정가로 매수(체결되지 않을 가격) -> 조회 -> 취소 -> 조회 순서로 한다."""
    from datetime import datetime
    from .broker.kis import KISBroker
    from .broker.base import Order, OrderState
    from .broker.ledger import OrderLedger, make_client_order_id
    from .ops.runner import Runner
    s = _settings(a)
    if s.mode != RunMode.KIS_VTS:
        s = Settings(**{**s.to_dict(), "mode": "kis_vts"})
    con = connect(s.db_path)
    results: dict[str, str] = {}
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    def step(name, fn):
        try:
            out = fn()
            results[name] = f"verified({stamp})"
            print(f"[OK]   {name}: {out}")
            return out
        except Exception as e:
            results[name] = f"failed({stamp}): {type(e).__name__}: {str(e)[:120]}"
            print(f"[FAIL] {name}: {type(e).__name__}: {e}")
            return None

    try:
        b = KISBroker()
    except Exception as e:
        print(f"[FAIL] 초기화: {e}")
        return
    step("token", lambda: "발급됨" if b._get_token() else "없음")
    acct = step("account", lambda: b.account())
    if acct:
        print(f"       예수금 {acct.cash:,.0f} / 주문가능 {acct.orderable_cash:,.0f} / 보유 {len(acct.positions)}종목")
        results["orderable"] = results["account"]
    q = step("quote", lambda: b.quote("005930", date.today().isoformat()))
    if a.with_order:
        hm = datetime.now().strftime("%H:%M")
        if not ("09:00" <= hm <= "15:15") or not TradingCalendar.load(a.calendar).is_trading_day(date.today()):
            print("[SKIP] 주문 검증은 거래일 09:00~15:15에만 실행합니다.")
        elif q and q.ref_price:
            from .broker.base import tick_floor
            ledger = OrderLedger(con, "kis_vts_verify")
            low = tick_floor(q.ref_price * 0.71) + 0   # 하한가(-30%) 근처: 체결되지 않을 가격
            o = Order(make_client_order_id("kis_vts_verify", "005930", "buy", "verify", datetime.now().isoformat()), "005930", "buy", 1, "limit", "verify",
                      f"{date.today()} verify", limit_price=low, reason="연동 검증용 주문(체결되지 않을 가격)")
            o = step("submit", lambda: ledger.submit(b, o))
            if o and o.state in (OrderState.SUBMITTED, OrderState.UNFILLED):
                results["submit"] = f"verified({stamp})"
                o = step("query", lambda: ledger.resolve(b, o))
                if o:
                    print(f"       조회 상태: {o.state.value}, 주문번호 {o.broker_order_id}")
                    step("cancel", lambda: ledger.apply(o, b.cancel(o)))
                    o2 = ledger.resolve(b, o)
                    print(f"       취소 후 상태: {o2.state.value}")
                    if o2.state != OrderState.CANCELLED:
                        results["cancel"] = f"failed({stamp}): 취소 후 상태 {o2.state.value} — 증권사 화면에서 확인 필요"
                        print("[WARN] 취소 완료가 확인되지 않았습니다. 증권사 모의투자 화면에서 주문을 직접 확인하세요.")
            elif o:
                results["submit"] = f"failed({stamp}): 상태 {o.state.value} {o.error}"
                print(f"[FAIL] submit: 상태 {o.state.value} {o.error}")
    else:
        print("[SKIP] 주문·조회·취소 검증은 --with-order 옵션으로 장중에 실행하세요.")
    prev = {}
    r = con.execute("SELECT value FROM ops_state WHERE key='kis_verification'").fetchone()
    if r:
        prev = json.loads(r[0])
    prev.update(results)
    con.execute("INSERT OR REPLACE INTO ops_state(key,value,updated_at) VALUES ('kis_verification',?,?)", (json.dumps(prev, ensure_ascii=False), stamp))
    con.commit()
    print("\n기능별 검증 기록:", json.dumps(prev, ensure_ascii=False, indent=1))


def cmd_kis_check(a):
    c = kis_credentials()
    print(f"KIS_APP_KEY={mask(c['app_key'])} KIS_APP_SECRET={mask(c['app_secret'])} KIS_ACCOUNT_NO={mask(c['account_no'])}")
    try:
        from .broker.kis import KISBroker
        b = KISBroker()
        acct = b.account()
        print(f"잔고 조회 성공: 예수금 {acct.cash:,.0f} 주문가능 {acct.orderable_cash:,.0f} 보유 {len(acct.positions)}종목")
        print("기능 상태:", b.feature_status(), "(이 세션에서 최초 성공 시 수동으로 verified로 갱신할 것)")
    except Exception as e:
        print(f"연결 실패: {type(e).__name__}: {e}")


def cmd_init_config(a):
    s = Settings()
    s.save(a.path)
    print(f"기본 설정 저장: {a.path}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="krflow", description="국내주식 수급 전략 검증·모의매매 (실계좌 차단)")
    p.add_argument("--config", default="config.json")
    p.add_argument("--db")
    p.add_argument("--mode", choices=[m.value for m in RunMode if m != RunMode.REAL])
    p.add_argument("--calendar", default="calendar/krx_holidays.csv", help="휴장일 CSV (date,kind,note)")
    sp = p.add_subparsers(dest="cmd", required=True)

    x = sp.add_parser("init-config"); x.add_argument("--path", default="config.json"); x.set_defaults(fn=cmd_init_config)
    x = sp.add_parser("synth", help="합성 데이터 생성"); x.add_argument("--start", required=True); x.add_argument("--end", required=True)
    x.add_argument("--codes", type=int, default=30); x.add_argument("--seed", type=int, default=42); x.add_argument("--flow-effect", type=float, default=0.0)
    x.add_argument("--inject-issues", action="store_true"); x.set_defaults(fn=cmd_synth)
    x = sp.add_parser("import-csv"); x.add_argument("--bars", required=True); x.add_argument("--flows"); x.set_defaults(fn=cmd_import_csv)
    x = sp.add_parser("collect-pykrx", help="[미검증] KRX 날짜별 전종목 시세·수급 수집 (KRX_ID/KRX_PW 필요)"); x.add_argument("--start", required=True); x.add_argument("--end", required=True)
    x.add_argument("--pause", type=float, default=1.0, help="날짜 사이 대기(초)"); x.set_defaults(fn=cmd_collect_pykrx)
    x = sp.add_parser("quality"); x.set_defaults(fn=cmd_quality)
    for name, fn in (("backtest", cmd_backtest), ("compare", cmd_compare)):
        x = sp.add_parser(name); x.add_argument("--start", required=True); x.add_argument("--end", required=True)
        x.add_argument("--out", default="results"); x.add_argument("--kind", choices=["synthetic", "real_backtest"])
        if name == "backtest":
            x.add_argument("--benchmark", help="벤치마크 코드(예: KOSPI 지수 시리즈를 bars로 넣은 경우)")
        else:
            x.add_argument("--split", help="학습/검증 분리 일자")
        x.set_defaults(fn=fn)
    x = sp.add_parser("paper", help="저장 데이터로 페이퍼 모의매매 시뮬레이션"); x.add_argument("--start", required=True); x.add_argument("--end", required=True); x.add_argument("-v", "--verbose", action="store_true"); x.set_defaults(fn=cmd_paper)
    x = sp.add_parser("phase", help="단계 1회 실행 (open|close|eod)"); x.add_argument("--phase", choices=["open", "sync_open", "close", "sync_close", "eod"], required=True); x.add_argument("--date"); x.set_defaults(fn=cmd_phase)
    x = sp.add_parser("serve", help="[미검증] KIS 모의투자 스케줄러"); x.set_defaults(fn=cmd_serve)
    x = sp.add_parser("dashboard"); x.add_argument("--port", type=int); x.add_argument("--out", default="results"); x.set_defaults(fn=cmd_dashboard)
    x = sp.add_parser("halt"); x.add_argument("--reason", default="사용자 수동 중단"); x.set_defaults(fn=cmd_halt)
    x = sp.add_parser("resume"); x.set_defaults(fn=cmd_resume)
    x = sp.add_parser("kis-check", help="[미검증] KIS 모의투자 접속 확인"); x.set_defaults(fn=cmd_kis_check)
    x = sp.add_parser("kis-verify", help="KIS 모의투자 기능별 실제 호출 검증 및 기록"); x.add_argument("--with-order", action="store_true", help="장중 체결되지 않을 가격으로 1주 주문->조회->취소"); x.set_defaults(fn=cmd_kis_verify)

    a = p.parse_args(argv)
    try:
        a.fn(a)
    except RealAccountBlocked as e:
        print(f"차단: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
