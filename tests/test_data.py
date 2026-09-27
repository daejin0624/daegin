import csv
from datetime import date

from krflow.config import Settings
from krflow.data.csv_source import load
from krflow.data.quality import blocked_codes
from helpers import synth_db


def test_synthetic_is_reproducible():
    _, a = synth_db(seed=3)
    _, b = synth_db(seed=3)
    _, c = synth_db(seed=4)
    assert a.fingerprint() == b.fingerprint() != c.fingerprint()


def test_quality_checks_flag_injected_issues_and_block_trading():
    con, store = synth_db(inject_issues=True)
    kinds = {r["kind"] for r in store.open_issues()}
    assert {"missing_bar", "provisional_flow", "unit_mismatch", "stale"} <= kinds
    d20 = store.dates()[20]
    blocked = blocked_codes(con, d20)
    assert "900002" in blocked and "900003" in blocked and "900005" in blocked


def test_revision_is_tracked_on_reingest():
    con, store = synth_db()
    from krflow.data.models import Bar
    d = store.dates()[5]
    b = store.bar("900000", d)
    store.upsert_bars([Bar("900000", d, b["open"], b["high"], b["low"], b["close"] + 1, b["volume"], b["value"], source="synthetic", asof_date=d, collected_at="x", available_at="x")])
    assert store.bar("900000", d)["revision"] == 1


def test_csv_partial_and_failed_are_not_ok(tmp_path):
    p = tmp_path / "bars.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["code", "date", "open", "high", "low", "close", "volume", "value"])
        w.writerow(["5930", "2025-01-02", 100, 110, 90, 105, 1000, 100000])
        w.writerow(["5930", "2025-01-02", 100, 110, 90, 105, 1000, 100000])  # 중복
    r = load(p, None)
    assert r.status == "partial" and r.bars[0].code == "005930" and "중복행 1건" in r.message
    assert load(tmp_path / "nope.csv", None).status == "failed"


def test_real_mode_blocked():
    import pytest
    from krflow.config import RealAccountBlocked
    with pytest.raises(RealAccountBlocked):
        Settings(mode="real")
