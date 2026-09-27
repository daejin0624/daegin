"""한국투자증권 REST 모의투자(VTS) 어댑터. [미검증]

이 코드는 공개 API 문서(https://apiportal.koreainvestment.com) 기준으로 작성됐으나, 이 개발 세션에서는
증권사 서버 접속이 불가해 실제 호출을 검증하지 못했다. 모든 기능이 'unverified'로 표시된다.
실계좌 도메인(openapi.koreainvestment.com:9443)은 코드에서 사용하지 않는다.

인증정보는 환경변수(KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT_NO)에서만 읽고, 로그/화면에 출력하지 않는다.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from ..config import kis_credentials, RealAccountBlocked
from ..data.models import now_iso
from .base import Broker, Order, OrderUpdate, OrderState, AccountSnapshot, BrokerTimeout, BrokerError

VTS_BASE = "https://openapivts.koreainvestment.com:29443"
TR = {  # 모의투자 TR ID (실전은 접두어 T; 여기서는 V만 허용)
    "buy": "VTTC0802U", "sell": "VTTC0801U", "cancel": "VTTC0803U",
    "balance": "VTTC8434R", "orderable": "VTTC8908R", "unfilled": "VTTC8036R", "daily_orders": "VTTC8001R",
}


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
        self._creds = creds
        self.timeout = timeout
        self._token: str | None = None
        self._token_exp = datetime.min
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
        r = requests.post(f"{VTS_BASE}/oauth2/tokenP", json={"grant_type": "client_credentials", "appkey": self._creds["app_key"],
                          "appsecret": self._creds["app_secret"]}, timeout=self.timeout)
        if r.status_code != 200:
            raise BrokerError(f"토큰 발급 실패 HTTP {r.status_code}")
        d = r.json()
        self._token = d["access_token"]
        self._token_exp = datetime.now() + timedelta(seconds=int(d.get("expires_in", 3600)) - 300)
        return self._token

    def _call(self, method: str, path: str, tr_id: str, **kw) -> dict:
        import requests
        try:
            r = requests.request(method, VTS_BASE + path, headers=self._headers(tr_id), timeout=self.timeout, **kw)
        except requests.Timeout as e:
            raise BrokerTimeout(str(e)) from e
        except requests.RequestException as e:
            raise BrokerTimeout(f"통신 장애: {type(e).__name__}") from e   # 전송 여부 불명 -> UNKNOWN
        if r.status_code != 200:
            raise BrokerError(f"HTTP {r.status_code}: {_safe(r.text)}")
        d = r.json()
        if d.get("rt_cd") != "0":
            raise BrokerError(f"{d.get('msg_cd')}: {d.get('msg1')}")
        return d

    # ---- Broker ----
    def submit(self, o: Order) -> OrderUpdate:
        body = {"CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt, "PDNO": o.code,
                "ORD_DVSN": "01" if o.order_type == "market" else "00",
                "ORD_QTY": str(o.qty), "ORD_UNPR": "0" if o.order_type == "market" else str(int(o.limit_price or 0))}
        d = self._call("POST", "/uapi/domestic-stock/v1/trading/order-cash", TR[o.side], data=json.dumps(body))
        return OrderUpdate(OrderState.SUBMITTED, broker_order_id=d["output"]["ODNO"])

    def query(self, o: Order) -> OrderUpdate:
        if not o.broker_order_id:
            # 전송 결과 불명: 당일 주문 조회에서 같은 종목/수량/방향을 찾는다
            d = self._call("GET", "/uapi/domestic-stock/v1/trading/inquire-daily-ccld", TR["daily_orders"], params={
                "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt, "INQR_STRT_DT": datetime.now().strftime("%Y%m%d"),
                "INQR_END_DT": datetime.now().strftime("%Y%m%d"), "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00", "PDNO": o.code,
                "CCLD_DVSN": "00", "ORD_GNO_BRNO": "", "ODNO": "", "INQR_DVSN_3": "00", "INQR_DVSN_1": "", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""})
            rows = [r for r in d.get("output1", []) if r.get("pdno") == o.code and int(r.get("ord_qty", 0)) == o.qty
                    and r.get("sll_buy_dvsn_cd") == ("02" if o.side == "buy" else "01")]
            if not rows:
                return OrderUpdate(OrderState.REJECTED, error="브로커에 해당 주문 없음(전송 안 됨으로 판단)")
            o.broker_order_id = rows[0]["odno"]
        d = self._call("GET", "/uapi/domestic-stock/v1/trading/inquire-daily-ccld", TR["daily_orders"], params={
            "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt, "INQR_STRT_DT": datetime.now().strftime("%Y%m%d"),
            "INQR_END_DT": datetime.now().strftime("%Y%m%d"), "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00", "PDNO": o.code,
            "CCLD_DVSN": "00", "ORD_GNO_BRNO": "", "ODNO": o.broker_order_id, "INQR_DVSN_3": "00", "INQR_DVSN_1": "", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""})
        rows = [r for r in d.get("output1", []) if r.get("odno") == o.broker_order_id]
        if not rows:
            return OrderUpdate(OrderState.UNKNOWN, broker_order_id=o.broker_order_id, error="조회 결과 없음")
        r = rows[0]
        filled, ordq = int(r.get("tot_ccld_qty", 0)), int(r.get("ord_qty", 0))
        avg = float(r.get("avg_prvs", 0) or 0) or None
        if r.get("cncl_yn") == "Y" or int(r.get("cncl_cfrm_qty", 0)) > 0:
            st = OrderState.CANCELLED
        elif filled >= ordq and ordq > 0:
            st = OrderState.FILLED
        elif filled > 0:
            st = OrderState.PARTIAL
        elif r.get("rjct_qty") and int(r["rjct_qty"]) > 0:
            st = OrderState.REJECTED
        else:
            st = OrderState.UNFILLED
        return OrderUpdate(st, filled, avg, o.broker_order_id)

    def cancel(self, o: Order) -> OrderUpdate:
        body = {"CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt, "KRX_FWDG_ORD_ORGNO": "", "ORGN_ODNO": o.broker_order_id,
                "ORD_DVSN": "01", "RVSE_CNCL_DVSN_CD": "02", "ORD_QTY": "0", "ORD_UNPR": "0", "QTY_ALL_ORD_YN": "Y"}
        self._call("POST", "/uapi/domestic-stock/v1/trading/order-rvsecncl", TR["cancel"], data=json.dumps(body))
        return OrderUpdate(OrderState.CANCEL_REQUESTED, o.filled_qty, o.avg_price, o.broker_order_id)

    def account(self) -> AccountSnapshot:
        d = self._call("GET", "/uapi/domestic-stock/v1/trading/inquire-balance", TR["balance"], params={
            "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt, "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""})
        pos = {r["pdno"]: (int(r["hldg_qty"]), float(r["pchs_avg_pric"])) for r in d.get("output1", []) if int(r.get("hldg_qty", 0)) > 0}
        sellable = {r["pdno"]: int(r.get("ord_psbl_qty", 0)) for r in d.get("output1", [])}
        o2 = (d.get("output2") or [{}])[0]
        cash = float(o2.get("dnca_tot_amt", 0))
        orderable = float(o2.get("prvs_rcdl_excc_amt", cash))
        return AccountSnapshot(cash, orderable, pos, sellable, now_iso(), True)

    def feature_status(self) -> dict[str, str]:
        return {k: "unverified" for k in ("token", "submit", "query", "cancel", "account")}


def _safe(text: str) -> str:
    """응답 본문에서 키/토큰이 섞여 나올 가능성을 막기 위해 길이 제한 후 반환."""
    return text[:200].replace("\n", " ")
