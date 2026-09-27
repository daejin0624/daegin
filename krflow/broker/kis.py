"""한국투자증권 REST 모의투자(VTS) 어댑터. [미검증]

공개 API 문서(https://apiportal.koreainvestment.com) 기준으로 작성했다. 개발 환경에서 증권사 서버에 접속할 수 없어
실제 호출은 검증하지 못했다. `python -m krflow.cli --mode kis_vts kis-verify`로 사용자 PC에서 기능별로 검증하고,
성공한 기능만 'verified'로 기록된다.

- 실계좌 도메인(openapi.koreainvestment.com:9443)과 실전 TR ID(T로 시작)는 사용하지 않는다.
- 인증정보는 환경변수(KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT_NO)에서만 읽고 로그/화면에 출력하지 않는다.
- 모의투자 API는 초당 호출 수 제한이 낮아 호출 간 최소 간격을 둔다.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

from ..config import kis_credentials, RealAccountBlocked
from ..data.models import now_iso
from .base import Broker, Order, OrderUpdate, OrderState, AccountSnapshot, BrokerTimeout, BrokerError, Quote

VTS_BASE = "https://openapivts.koreainvestment.com:29443"
TR = {  # 모의투자 TR ID
    "buy": "VTTC0802U", "sell": "VTTC0801U", "cancel": "VTTC0803U",
    "balance": "VTTC8434R", "orderable": "VTTC8908R", "daily_orders": "VTTC8001R",
}
QUOTE_TR = "FHKST01010100"   # 주식현재가 시세 (시세 조회 TR은 실전/모의 공통)
MIN_INTERVAL = 0.6           # 초. 모의투자 초당 호출 제한 대응


def _split_id(broker_order_id: str | None) -> tuple[str, str]:
    """broker_order_id는 '주문조직번호-주문번호'로 저장한다(취소에 둘 다 필요)."""
    if not broker_order_id:
        return "", ""
    if "-" in broker_order_id:
        org, odno = broker_order_id.split("-", 1)
        return org, odno
    return "", broker_order_id


class KISBroker(Broker):
    name = "kis_vts"
    verified = False

    def __init__(self, timeout: float = 10.0):
        try:
            import requests  # noqa: F401
        except ImportError as e:
            raise BrokerError("requests 미설치") from e
        creds = kis_credentials()
        if not all(creds.values()):
            raise BrokerError("KIS_APP_KEY / KIS_APP_SECRET / KIS_ACCOUNT_NO 환경변수 필요")
        for tr in TR.values():
            if not tr.startswith("V"):
                raise RealAccountBlocked("실전 TR ID 사용 차단")
        if "openapivts" not in VTS_BASE:
            raise RealAccountBlocked("모의투자 도메인만 허용")
        self._creds = creds
        self.timeout = timeout
        self._token: str | None = None
        self._token_exp = datetime.min
        self._last_call = 0.0
        acct = creds["account_no"].replace(" ", "")
        self.cano, self.acnt_prdt = (acct.split("-") + ["01"])[:2]

    # ---- HTTP ----
    def _headers(self, tr_id: str) -> dict:
        return {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {self._get_token()}",
                "appkey": self._creds["app_key"], "appsecret": self._creds["app_secret"], "tr_id": tr_id, "custtype": "P"}

    def _get_token(self) -> str:
        import requests
        if self._token and datetime.now() < self._token_exp:
            return self._token
        try:
            r = requests.post(f"{VTS_BASE}/oauth2/tokenP", json={"grant_type": "client_credentials", "appkey": self._creds["app_key"],
                              "appsecret": self._creds["app_secret"]}, timeout=self.timeout)
        except requests.RequestException as e:
            raise BrokerError(f"토큰 발급 통신 실패: {type(e).__name__}") from e
        if r.status_code != 200:
            raise BrokerError(f"토큰 발급 실패 HTTP {r.status_code}: {_safe(r.text)}")
        d = r.json()
        self._token = d["access_token"]
        self._token_exp = datetime.now() + timedelta(seconds=int(d.get("expires_in", 3600)) - 300)
        return self._token

    def _call(self, method: str, path: str, tr_id: str, order: bool = False, **kw) -> dict:
        """order=True(주문·취소)이면 전송 후 응답을 못 받은 경우 BrokerTimeout(결과 불명)으로 올린다."""
        import requests
        headers = self._headers(tr_id)
        wait = MIN_INTERVAL - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        try:
            r = requests.request(method, VTS_BASE + path, headers=headers, timeout=self.timeout, **kw)
        except requests.ConnectionError as e:
            if order:
                raise BrokerTimeout(f"통신 장애: {type(e).__name__}") from e   # 전송 여부 불명
            raise BrokerError(f"통신 장애: {type(e).__name__}") from e
        except requests.Timeout as e:
            if order:
                raise BrokerTimeout("응답 시간 초과") from e
            raise BrokerError("응답 시간 초과") from e
        finally:
            self._last_call = time.monotonic()
        if r.status_code != 200:
            raise BrokerError(f"HTTP {r.status_code}: {_safe(r.text)}")
        d = r.json()
        if d.get("rt_cd") != "0":
            raise BrokerError(f"{d.get('msg_cd')}: {d.get('msg1')}")
        return d

    def _daily_orders(self, code: str, odno: str = "") -> list[dict]:
        today = datetime.now().strftime("%Y%m%d")
        d = self._call("GET", "/uapi/domestic-stock/v1/trading/inquire-daily-ccld", TR["daily_orders"], params={
            "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt, "INQR_STRT_DT": today, "INQR_END_DT": today,
            "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00", "PDNO": code, "CCLD_DVSN": "00", "ORD_GNO_BRNO": "", "ODNO": odno,
            "INQR_DVSN_3": "00", "INQR_DVSN_1": "", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""})
        return d.get("output1", []) or []

    # ---- Broker ----
    def submit(self, o: Order) -> OrderUpdate:
        if o.order_type == "limit":
            dvsn, price = "00", str(int(o.limit_price or 0))
        else:
            dvsn, price = "01", "0"
        body = {"CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt, "PDNO": o.code, "ORD_DVSN": dvsn,
                "ORD_QTY": str(o.qty), "ORD_UNPR": price}
        d = self._call("POST", "/uapi/domestic-stock/v1/trading/order-cash", TR[o.side], order=True, data=json.dumps(body))
        out = d.get("output", {})
        return OrderUpdate(OrderState.SUBMITTED, broker_order_id=f"{out.get('KRX_FWDG_ORD_ORGNO', '')}-{out['ODNO']}")

    def query(self, o: Order) -> OrderUpdate:
        org, odno = _split_id(o.broker_order_id)
        if not odno:
            # 전송 결과 불명: 당일 주문 중 같은 종목·수량·방향을 찾는다 (설계상 종목·방향별 하루 1건)
            side = "02" if o.side == "buy" else "01"
            rows = [r for r in self._daily_orders(o.code) if r.get("pdno") == o.code and int(r.get("ord_qty", 0)) == o.qty
                    and r.get("sll_buy_dvsn_cd") == side]
            if not rows:
                return OrderUpdate(OrderState.REJECTED, error="증권사에 해당 주문 없음(전송 안 됨으로 판단)")
            odno, org = rows[0]["odno"], rows[0].get("ord_gno_brno", "")
        rows = [r for r in self._daily_orders(o.code, odno) if r.get("odno") == odno]
        bid = f"{org}-{odno}"
        if not rows:
            return OrderUpdate(OrderState.UNKNOWN, broker_order_id=bid, error="조회 결과 없음")
        r = rows[0]
        org = org or r.get("ord_gno_brno", "")
        bid = f"{org}-{odno}"
        filled, ordq = int(r.get("tot_ccld_qty", 0) or 0), int(r.get("ord_qty", 0) or 0)
        avg = float(r.get("avg_prvs", 0) or 0) or None
        cancelled = int(r.get("cnc_cfrm_qty", r.get("cncl_cfrm_qty", 0)) or 0)
        if r.get("cncl_yn") == "Y" or cancelled > 0:
            st = OrderState.CANCELLED
        elif ordq > 0 and filled >= ordq:
            st = OrderState.FILLED
        elif int(r.get("rjct_qty", 0) or 0) > 0:
            st = OrderState.REJECTED
        elif filled > 0:
            st = OrderState.PARTIAL
        else:
            st = OrderState.UNFILLED
        return OrderUpdate(st, filled, avg, bid)

    def cancel(self, o: Order) -> OrderUpdate:
        org, odno = _split_id(o.broker_order_id)
        body = {"CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt, "KRX_FWDG_ORD_ORGNO": org, "ORGN_ODNO": odno,
                "ORD_DVSN": "00", "RVSE_CNCL_DVSN_CD": "02", "ORD_QTY": "0", "ORD_UNPR": "0", "QTY_ALL_ORD_YN": "Y"}
        self._call("POST", "/uapi/domestic-stock/v1/trading/order-rvsecncl", TR["cancel"], order=True, data=json.dumps(body))
        return OrderUpdate(OrderState.CANCEL_REQUESTED, o.filled_qty, o.avg_price, o.broker_order_id)

    def account(self) -> AccountSnapshot:
        d = self._call("GET", "/uapi/domestic-stock/v1/trading/inquire-balance", TR["balance"], params={
            "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt, "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""})
        rows = d.get("output1", []) or []
        pos = {r["pdno"]: (int(r["hldg_qty"]), float(r["pchs_avg_pric"])) for r in rows if int(r.get("hldg_qty", 0) or 0) > 0}
        sellable = {r["pdno"]: int(r.get("ord_psbl_qty", 0) or 0) for r in rows}
        o2 = (d.get("output2") or [{}])[0]
        cash = float(o2.get("dnca_tot_amt", 0) or 0)
        return AccountSnapshot(cash, self.orderable_cash(), pos, sellable, now_iso(), True)

    def orderable_cash(self, code: str = "005930") -> float:
        """매수가능조회. 미체결 매수 주문 금액이 이미 차감된 값이다."""
        d = self._call("GET", "/uapi/domestic-stock/v1/trading/inquire-psbl-order", TR["orderable"], params={
            "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt, "PDNO": code, "ORD_UNPR": "", "ORD_DVSN": "01",
            "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N"})
        return float(d.get("output", {}).get("ord_psbl_cash", 0) or 0)

    def quote(self, code: str, date: str) -> Quote:
        d = self._call("GET", "/uapi/domestic-stock/v1/quotations/inquire-price", QUOTE_TR,
                       params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
        out = d.get("output", {})
        # stck_sdpr = 기준가(전일 종가), temp_stop_yn = 거래정지 여부
        ref = float(out.get("stck_sdpr", 0) or 0) or None
        return Quote(code, ref, out.get("temp_stop_yn") == "Y", now_iso(), "kis_vts")

    def feature_status(self) -> dict[str, str]:
        return {k: "unverified" for k in ("token", "account", "orderable", "quote", "submit", "query", "cancel")}


def _safe(text: str) -> str:
    """응답 본문은 길이 제한 후 기록한다."""
    return text[:200].replace("\n", " ")
