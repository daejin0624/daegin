"""한투 VTS 어댑터를 가짜 HTTP 응답으로 검증한다. 실제 서버 응답 형식과의 일치는 kis-verify로 사용자 PC에서 확인한다."""
import json

import pytest
import requests

from krflow.broker import kis
from krflow.broker.base import Order, OrderState, BrokerTimeout, BrokerError


class Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code, self.text = body, status, json.dumps(body)

    def json(self):
        return self.body


@pytest.fixture
def broker(monkeypatch):
    monkeypatch.setenv("KIS_APP_KEY", "APPKEY1234")
    monkeypatch.setenv("KIS_APP_SECRET", "SECRET5678")
    monkeypatch.setenv("KIS_ACCOUNT_NO", "50123456-01")
    monkeypatch.setattr(kis, "MIN_INTERVAL", 0)
    monkeypatch.setattr(requests, "post", lambda *a, **k: Resp({"access_token": "tok", "expires_in": 86400}))
    calls = []
    routes = {}

    def fake_request(method, url, headers=None, timeout=None, **kw):
        calls.append({"method": method, "url": url, "headers": headers, **kw})
        for key, fn in routes.items():
            if key in url:
                return fn(kw)
        raise AssertionError(f"unexpected {url}")
    monkeypatch.setattr(requests, "request", fake_request)
    b = kis.KISBroker()
    b.routes, b.calls = routes, calls
    return b


def _order(**kw):
    d = dict(client_order_id="c1", code="005930", side="buy", qty=3, order_type="limit", intent="entry", scheduled_for="2026-01-05 open", limit_price=70000)
    d.update(kw)
    return Order(**d)


def test_submit_uses_vts_tr_and_stores_org_and_order_no(broker):
    broker.routes["order-cash"] = lambda kw: Resp({"rt_cd": "0", "output": {"KRX_FWDG_ORD_ORGNO": "91252", "ODNO": "0000117057"}})
    u = broker.submit(_order())
    assert u.state == OrderState.SUBMITTED and u.broker_order_id == "91252-0000117057"
    c = broker.calls[-1]
    assert c["headers"]["tr_id"] == "VTTC0802U" and "openapivts" in c["url"]
    body = json.loads(c["data"])
    assert body["ORD_DVSN"] == "00" and body["ORD_UNPR"] == "70000" and body["CANO"] == "50123456" and body["ACNT_PRDT_CD"] == "01"


def test_order_timeout_is_unknown_but_query_timeout_is_error(broker):
    def boom(kw):
        raise requests.Timeout("t")
    broker.routes["order-cash"] = boom
    with pytest.raises(BrokerTimeout):
        broker.submit(_order())
    broker.routes["inquire-daily-ccld"] = boom
    with pytest.raises(BrokerError) as ei:
        broker.query(_order(broker_order_id="1-2"))
    assert not isinstance(ei.value, BrokerTimeout)


@pytest.mark.parametrize("row,state", [
    ({"tot_ccld_qty": "3", "ord_qty": "3", "avg_prvs": "70100"}, OrderState.FILLED),
    ({"tot_ccld_qty": "1", "ord_qty": "3", "avg_prvs": "70100"}, OrderState.PARTIAL),
    ({"tot_ccld_qty": "0", "ord_qty": "3"}, OrderState.UNFILLED),
    ({"tot_ccld_qty": "1", "ord_qty": "3", "cnc_cfrm_qty": "2", "avg_prvs": "70100"}, OrderState.CANCELLED),
    ({"tot_ccld_qty": "0", "ord_qty": "3", "rjct_qty": "3"}, OrderState.REJECTED),
])
def test_query_state_mapping(broker, row, state):
    broker.routes["inquire-daily-ccld"] = lambda kw: Resp({"rt_cd": "0", "output1": [{"odno": "0000117057", "pdno": "005930", **row}]})
    u = broker.query(_order(broker_order_id="91252-0000117057"))
    assert u.state == state


def test_unknown_order_is_found_by_code_qty_side(broker):
    broker.routes["inquire-daily-ccld"] = lambda kw: Resp({"rt_cd": "0", "output1": [
        {"odno": "555", "ord_gno_brno": "91252", "pdno": "005930", "ord_qty": "3", "sll_buy_dvsn_cd": "02", "tot_ccld_qty": "3", "avg_prvs": "70000"}]})
    u = broker.query(_order(broker_order_id=None))
    assert u.state == OrderState.FILLED and u.broker_order_id == "91252-555"
    broker.routes["inquire-daily-ccld"] = lambda kw: Resp({"rt_cd": "0", "output1": []})
    assert broker.query(_order(broker_order_id=None)).state == OrderState.REJECTED


def test_cancel_sends_org_number(broker):
    broker.routes["order-rvsecncl"] = lambda kw: Resp({"rt_cd": "0", "output": {}})
    u = broker.cancel(_order(broker_order_id="91252-0000117057"))
    body = json.loads(broker.calls[-1]["data"])
    assert body["KRX_FWDG_ORD_ORGNO"] == "91252" and body["ORGN_ODNO"] == "0000117057" and body["RVSE_CNCL_DVSN_CD"] == "02"
    assert u.state == OrderState.CANCEL_REQUESTED and broker.calls[-1]["headers"]["tr_id"] == "VTTC0803U"


def test_account_quote_and_api_error(broker):
    broker.routes["inquire-balance"] = lambda kw: Resp({"rt_cd": "0", "output1": [
        {"pdno": "005930", "hldg_qty": "10", "pchs_avg_pric": "70000.0", "ord_psbl_qty": "10"}], "output2": [{"dnca_tot_amt": "5000000"}]})
    broker.routes["inquire-psbl-order"] = lambda kw: Resp({"rt_cd": "0", "output": {"ord_psbl_cash": "4300000"}})
    broker.routes["inquire-price"] = lambda kw: Resp({"rt_cd": "0", "output": {"stck_sdpr": "71000", "temp_stop_yn": "N"}})
    a = broker.account()
    assert a.cash == 5_000_000 and a.orderable_cash == 4_300_000 and a.positions["005930"] == (10, 70000.0) and a.verified_live
    q = broker.quote("005930", "2026-01-05")
    assert q.ref_price == 71000 and not q.halted
    broker.routes["inquire-price"] = lambda kw: Resp({"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과"})
    with pytest.raises(BrokerError, match="EGW00201"):
        broker.quote("005930", "2026-01-05")


def test_secrets_not_in_errors(broker):
    broker.routes["inquire-price"] = lambda kw: Resp({"error": "bad"}, status=500)
    with pytest.raises(BrokerError) as ei:
        broker.quote("005930", "2026-01-05")
    assert "SECRET5678" not in str(ei.value) and "APPKEY1234" not in str(ei.value)
