"""pykrx(KRX 정보데이터시스템) 날짜별 전종목 수집기. [미검증: 개발 환경에서 KRX 접속 불가]

- pykrx 1.2.x는 KRX 로그인이 필요하다. 환경변수 KRX_ID, KRX_PW를 설정해야 한다
  (data.krx.co.kr 회원가입). 로그인 실패 시 pykrx는 빈 표를 돌려줄 수 있으므로,
  거래일인데 빈 결과면 '실패'로 기록한다.
- 날짜별 전종목 조회라서 그날 상장돼 있던 종목이 모두 들어온다. 이후 상장폐지된 종목도 포함되므로
  '현재 상장 종목으로 과거를 돌리는' 생존편향이 생기지 않는다.
- 시세는 원주가(수정주가 아님). 분할·병합일의 급변은 품질검사(abnormal_jump)로 걸러진다.
- 수급은 거래대금(원) 기준 순매수. 외국인 = '외국인' + '기타외국인'.
- 확인 가능 시각 가정: 기준일 18:00. 같은 날 18:00 이전에 수집하면 잠정치로 저장한다.
"""
from __future__ import annotations

import time
from datetime import date, datetime

from ..calendar import TradingCalendar
from .models import Bar, Flow, CollectionResult, now_iso

SOURCE = "pykrx"
VERIFIED = False
MARKETS = ("KOSPI", "KOSDAQ")


def _api():
    from pykrx import stock  # type: ignore
    return stock


def fetch_day(d: date, api=None, now: datetime | None = None) -> tuple[CollectionResult, list[tuple]]:
    """하루치 전종목 시세·수급. 반환: (수집결과, universe 행[(code,name,market,None,None)])."""
    ds, ymd = d.isoformat(), d.strftime("%Y%m%d")
    now = now or datetime.now()
    collected = now.replace(microsecond=0).isoformat(sep=" ")
    final_at = f"{ds} 18:00:00"
    is_final = collected >= final_at
    available_at = max(final_at, collected) if not is_final else final_at
    try:
        api = api or _api()
    except ImportError:
        return CollectionResult(SOURCE, "bars+flows", ds, ds, 0, 0, "unsupported", "pykrx 미설치: pip install pykrx"), []
    bars: list[Bar] = []
    flows: list[Flow] = []
    universe: list[tuple] = []
    problems: list[str] = []
    ok_markets = 0
    for mkt in MARKETS:
        try:
            ohlcv = api.get_market_ohlcv_by_ticker(ymd, mkt)
            inst = api.get_market_net_purchases_of_equities_by_ticker(ymd, ymd, mkt, "기관합계")
            frn = api.get_market_net_purchases_of_equities_by_ticker(ymd, ymd, mkt, "외국인")
            frn2 = api.get_market_net_purchases_of_equities_by_ticker(ymd, ymd, mkt, "기타외국인")
        except Exception as ex:  # 네트워크/로그인/파싱 실패
            problems.append(f"{mkt}: {type(ex).__name__}: {str(ex)[:100]}")
            continue
        if ohlcv is None or len(ohlcv) == 0 or (ohlcv[["시가", "고가", "저가", "종가"]] == 0).all(axis=None):
            problems.append(f"{mkt}: 시세 빈 결과")
            continue
        ok_markets += 1
        if inst is None or len(inst) == 0:
            problems.append(f"{mkt}: 기관 수급 빈 결과")
        if frn is None or len(frn) == 0:
            problems.append(f"{mkt}: 외국인 수급 빈 결과")
        names = {}
        for df in (inst, frn):
            if df is not None and len(df) and "종목명" in df.columns:
                names.update(df["종목명"].to_dict())
        inst_v = inst["순매수거래대금"].to_dict() if inst is not None and len(inst) else {}
        frn_v = frn["순매수거래대금"].to_dict() if frn is not None and len(frn) else {}
        frn2_v = frn2["순매수거래대금"].to_dict() if frn2 is not None and len(frn2) else {}
        for code, row in ohlcv.iterrows():
            code = str(code).zfill(6)
            o, h, l, c = (float(row[k]) for k in ("시가", "고가", "저가", "종가"))
            vol = float(row["거래량"])
            halted = vol == 0 and o == 0     # pykrx: 거래정지 종목은 종가만 있고 나머지는 0
            bars.append(Bar(code, ds, o or None, h or None, l or None, c or None, vol, float(row["거래대금"]), 1.0,
                            halted, not halted, not halted, SOURCE, ds, collected, final_at))
            universe.append((code, names.get(code), mkt, None, None))
            if code in inst_v and code in frn_v:
                flows.append(Flow(code, ds, float(inst_v[code]), float(frn_v[code]) + float(frn2_v.get(code, 0.0)), None,
                                  "KRW", is_final, SOURCE, ds, collected, available_at))
            # 수급이 없는 종목은 저장하지 않는다 -> 품질검사에서 missing_flow로 차단
    requested, received = len(MARKETS), ok_markets
    if not bars:
        status = "failed"
    elif problems:
        status = "partial"
    else:
        status = "ok"
    msg = "; ".join(problems) or f"{len(bars)}종목"
    if not is_final:
        msg += "; 18:00 이전 수집 -> 수급 잠정치"
    return CollectionResult(SOURCE, "bars+flows", ds, ds, requested, received, status, msg, bars, flows), universe


def collect_range(store, start: date, end: date, cal: TradingCalendar, api=None, pause: float = 1.0,
                  skip_existing: bool = True, log=print) -> dict:
    """거래일마다 fetch_day. 날짜별로 collection_log에 남겨 부분 실패가 드러나게 한다. 중단 후 재실행하면 이어서 받는다."""
    # 마지막 수집 기록이 ok인 날만 건너뛴다. 부분 수집(partial)·실패일은 다시 받는다.
    done = {r[0] for r in store.con.execute(
        "SELECT date_from FROM collection_log c WHERE source=? AND id=(SELECT MAX(id) FROM collection_log WHERE source=? AND date_from=c.date_from) AND status='ok'",
        (SOURCE, SOURCE))} if skip_existing else set()
    summary = {"ok": 0, "partial": 0, "failed": 0, "unsupported": 0, "skipped": 0}
    for d in cal.trading_days(start, end):
        if d.isoformat() in done:
            summary["skipped"] += 1
            continue
        r, uni = fetch_day(d, api)
        store.ingest(r)
        if uni:
            store.upsert_universe_seen(uni, d.isoformat())
        summary[r.status] += 1
        log(f"{d} [{r.status}] {r.message}")
        if r.status == "unsupported":
            break
        if pause:
            time.sleep(pause)   # KRX 서버 부하·차단 방지
    return summary
