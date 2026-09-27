from datetime import date

from krflow.db import connect
from krflow.data.store import MarketStore
from krflow.data.synthetic import generate
from krflow.data.quality import run_quality_checks


def synth_db(start="2024-01-01", end="2024-12-31", codes=20, seed=1, flow_effect=0.0, inject_issues=False):
    con = connect(":memory:")
    store = MarketStore(con)
    store.ingest(generate(date.fromisoformat(start), date.fromisoformat(end), codes, seed, flow_effect, inject_issues=inject_issues))
    run_quality_checks(con)
    return con, store
