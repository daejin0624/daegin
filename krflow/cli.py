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
    s = _settings(a)
    con = connect(s.db_path)
    codes = a.codes.split(",")
    r = pykrx_source.fetch(date.fromisoformat(a.start), date.fromisoformat(a.end), codes)
    MarketStore(con).ingest(r)
    print(f"[{r.status}] {r.message}; bars {len(r.bars)} flows {len(r.flows)} (어댑터 검증 상태: {'검증' if pykrx_source.VERIFIED else '미검증'})")
    if r.bars:
        print(f"품질 이슈 {len(run_quality_checks(con))}건")


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


def _make_runner(s: Settings, con):
    from .ops.runner import Runner
    if s.mode == RunMode.KIS_VTS:
        from .broker.kis import KISBroker
        broker = KISBroker()
    else:
        from .broker.paper import PaperBroker
        broker = PaperBroker(MarketStore(con), s.costs, s.risk.initial_cash)
    r = Runner(s, con, broker)
    r._set_state("broker_features", broker.feature_status())
    return r


def cmd_paper(a):
    s = _settings(a)
    if s.mode not in (RunMode.PAPER, RunMode.SYNTHETIC):
        s = Settings(**{**s.to_dict(), "mode": "paper"})
    con = connect(s.db_path)
    r = _make_runner(s, con)
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
    r = _make_runner(s, con)
    d = a.date or date.today().isoformat()
    rep = {"open": r.run_open, "close": r.run_close, "eod": r.run_eod}[a.phase](d)
    print(json.dumps(rep.__dict__, ensure_ascii=False, indent=1))


def cmd_serve(a):
    """KIS 모의투자용 스케줄러 [미검증]. 거래일 09:01 open, 15:20 close(동시호가 전 시장가), 18:30 eod."""
    import time
    from datetime import datetime
    s = _settings(a)
    con = connect(s.db_path)
    r = _make_runner(s, con)
    cal = TradingCalendar.load(a.calendar)
    print(f"스케줄러 시작 (모드 {s.mode.value}, 실계좌 차단). Ctrl+C로 종료")
    while True:
        now = datetime.now()
        d = now.date()
        if cal.is_trading_day(d):
            hm = now.strftime("%H:%M")
            for phase, t in (("open", "09:01"), ("close", "15:20"), ("eod", "18:30")):
                if hm >= t and not r._done(d.isoformat(), phase):
                    rep = getattr(r, f"run_{phase}")(d.isoformat())
                    print(f"{now:%H:%M} {phase}: {rep.actions} {rep.blocked}")
        time.sleep(30)


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
    sp = p.add_subparsers(dest="cmd", required=True)

    x = sp.add_parser("init-config"); x.add_argument("--path", default="config.json"); x.set_defaults(fn=cmd_init_config)
    x = sp.add_parser("synth", help="합성 데이터 생성"); x.add_argument("--start", required=True); x.add_argument("--end", required=True)
    x.add_argument("--codes", type=int, default=30); x.add_argument("--seed", type=int, default=42); x.add_argument("--flow-effect", type=float, default=0.0)
    x.add_argument("--inject-issues", action="store_true"); x.add_argument("--calendar"); x.set_defaults(fn=cmd_synth)
    x = sp.add_parser("import-csv"); x.add_argument("--bars", required=True); x.add_argument("--flows"); x.set_defaults(fn=cmd_import_csv)
    x = sp.add_parser("collect-pykrx", help="[미검증] pykrx로 실제 데이터 수집"); x.add_argument("--start", required=True); x.add_argument("--end", required=True); x.add_argument("--codes", required=True); x.set_defaults(fn=cmd_collect_pykrx)
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
    x = sp.add_parser("phase", help="단계 1회 실행 (open|close|eod)"); x.add_argument("--phase", choices=["open", "close", "eod"], required=True); x.add_argument("--date"); x.set_defaults(fn=cmd_phase)
    x = sp.add_parser("serve", help="[미검증] KIS 모의투자 스케줄러"); x.add_argument("--calendar"); x.set_defaults(fn=cmd_serve)
    x = sp.add_parser("dashboard"); x.add_argument("--port", type=int); x.add_argument("--out", default="results"); x.set_defaults(fn=cmd_dashboard)
    x = sp.add_parser("halt"); x.add_argument("--reason", default="사용자 수동 중단"); x.set_defaults(fn=cmd_halt)
    x = sp.add_parser("resume"); x.set_defaults(fn=cmd_resume)
    x = sp.add_parser("kis-check", help="[미검증] KIS 모의투자 접속 확인"); x.set_defaults(fn=cmd_kis_check)

    a = p.parse_args(argv)
    try:
        a.fn(a)
    except RealAccountBlocked as e:
        print(f"차단: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
