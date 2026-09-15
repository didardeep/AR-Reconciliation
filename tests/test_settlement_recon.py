import pandas as pd
from app.core.enums import ExceptionCode
from app.reconciliation.settlement import SettlementReconciliation


def _by_id(res):
    return res.frame.set_index("order_release_id")


def test_forward_only(settle_fwd):
    res = SettlementReconciliation().reconcile(settle_fwd, None)
    row = _by_id(res).loc["OR002"]
    assert row.forward_customer_paid_amt == 500
    assert row.reverse_customer_paid_amt == 0
    assert row.net_settlement == 500


def test_forward_plus_reverse(settle_fwd, settle_rev):
    res = SettlementReconciliation().reconcile(settle_fwd, settle_rev)
    d = _by_id(res)
    assert d.loc["OR001"].net_settlement == 800          # 1000 - 200
    assert d.loc["OR003"].forward_customer_paid_amt == 700  # 400+300 aggregated
    assert d.loc["OR003"].net_settlement == 600          # 700 - 100


def test_duplicate_order_release_id_flagged(settle_fwd):
    res = SettlementReconciliation().reconcile(settle_fwd, None)
    assert ExceptionCode.DUPLICATE_ORDER_RELEASE_ID in {e.code for e in res.collector.records}


def test_reverse_only_record():
    fwd = pd.DataFrame({"order_release_id": ["OR1"], "customer_paid_amt": [100]})
    rev = pd.DataFrame({"order_release_id": ["OR2"], "customer_paid_amt": [50]})
    res = SettlementReconciliation().reconcile(fwd, rev)
    assert ExceptionCode.SETTLEMENT_REVERSE_ONLY in {e.code for e in res.collector.records}


def test_missing_order_release_id():
    fwd = pd.DataFrame({"order_release_id": [None], "customer_paid_amt": [100]})
    res = SettlementReconciliation().reconcile(fwd, None)
    assert ExceptionCode.MISSING_ORDER_RELEASE_ID in {e.code for e in res.collector.records}


def test_reverse_gt_forward():
    fwd = pd.DataFrame({"order_release_id": ["OR1"], "customer_paid_amt": [100]})
    rev = pd.DataFrame({"order_release_id": ["OR1"], "customer_paid_amt": [250]})
    res = SettlementReconciliation().reconcile(fwd, rev)
    assert ExceptionCode.SETTLEMENT_REVERSE_GT_FORWARD in {e.code for e in res.collector.records}
