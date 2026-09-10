import pandas as pd
from app.core.enums import ExceptionCode, MatchStatus
from app.reconciliation.final import FinalReconciliation

SALES = pd.DataFrame({"packet_id": ["P1", "P2", "P3"], "net_sales": [1000.0, 500.0, 700.0]})
SETTLE = pd.DataFrame({"order_release_id": ["OR1", "OR2", "OR3"],
                       "net_settlement": [1000.0, 500.4, 690.0]})
LINK = pd.DataFrame({"packet_id": ["P1", "P2", "P3"], "order_release_id": ["OR1", "OR2", "OR3"]})


def test_exact_match():
    res = FinalReconciliation().reconcile(SALES, SETTLE, link=LINK, tolerance=1.0)
    d = res.frame.set_index("packet_id")
    assert d.loc["P1"].status == MatchStatus.MATCHED.value


def test_within_tolerance():
    res = FinalReconciliation().reconcile(SALES, SETTLE, link=LINK, tolerance=1.0)
    assert res.frame.set_index("packet_id").loc["P2"].status == MatchStatus.MATCHED.value  # diff 0.4


def test_outside_tolerance():
    res = FinalReconciliation().reconcile(SALES, SETTLE, link=LINK, tolerance=1.0)
    row = res.frame.set_index("packet_id").loc["P3"]                     # diff 10
    assert row.status == MatchStatus.EXCEPTION.value
    assert row.exception_type == ExceptionCode.AMOUNT_MISMATCH.value


def test_missing_mapping_flags_all():
    res = FinalReconciliation().reconcile(SALES, SETTLE, link=None, settlement_packet_map=None, tolerance=1.0)
    assert (res.frame.exception_type == ExceptionCode.MISSING_MAPPING.value).all()
    assert ExceptionCode.MISSING_MAPPING in {e.code for e in res.collector.records}


def test_sales_only_and_settlement_only():
    sales = pd.DataFrame({"packet_id": ["P1"], "net_sales": [100.0]})
    settle = pd.DataFrame({"order_release_id": ["OR2"], "net_settlement": [100.0]})
    link = pd.DataFrame({"packet_id": ["P1"], "order_release_id": ["OR1"]})
    res = FinalReconciliation().reconcile(sales, settle, link=link, tolerance=1.0)
    types = set(res.frame.exception_type.dropna())
    assert ExceptionCode.SALES_ONLY.value in types
    assert ExceptionCode.SETTLEMENT_ONLY.value in types
