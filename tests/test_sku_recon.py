import pandas as pd
from app.core.enums import ExceptionCode
from app.reconciliation.sku import SkuReconciliation


def _by_sku(res):
    return res.frame.set_index(["packet_id", "sku_code"])


def test_clean_partial_return(fwd_sku, rt_sku, rto_sku):
    # P001/SKU-A returned via RT, P001/SKU-B returned via RTO -- a split
    # packet on different SKUs, legitimate.
    res = SkuReconciliation().reconcile(fwd_sku, rt_sku, rto_sku)
    d = _by_sku(res)
    row_a = d.loc[("P001", "SKU-A")]
    assert row_a.rt_qty == 1
    assert row_a.rto_qty == 0
    assert row_a.flag is None
    codes = {e.code for e in res.collector.records}
    assert ExceptionCode.PACKET_SPLIT_RT_RTO in codes
    assert res.packet_flags == {}
    assert res.withheld == set()


def test_returned_gt_ordered():
    fwd = pd.DataFrame({"packet_id": ["P1"], "sku_code": ["SKU-A"], "qty": [4]})
    rt = pd.DataFrame({"packet_id": ["P1"], "sku_code": ["SKU-A"], "qty": [5]})
    res = SkuReconciliation().reconcile(fwd, rt, None)
    row = _by_sku(res).loc[("P1", "SKU-A")]
    assert row.flag == ExceptionCode.SKU_RETURNED_GT_ORDERED.value
    assert ("P1", "SKU-A") in res.withheld
    assert res.packet_flags["P1"] == ExceptionCode.SKU_RETURNED_GT_ORDERED.value
    codes = {e.code for e in res.collector.records}
    assert ExceptionCode.SKU_RETURNED_GT_ORDERED in codes


def test_same_sku_in_both_rt_and_rto():
    fwd = pd.DataFrame({"packet_id": ["P1"], "sku_code": ["SKU-A"], "qty": [4]})
    rt = pd.DataFrame({"packet_id": ["P1"], "sku_code": ["SKU-A"], "qty": [1]})
    rto = pd.DataFrame({"packet_id": ["P1"], "sku_code": ["SKU-A"], "qty": [1]})
    res = SkuReconciliation().reconcile(fwd, rt, rto)
    row = _by_sku(res).loc[("P1", "SKU-A")]
    assert row.flag == ExceptionCode.RT_RTO_AMBIGUITY.value
    assert ("P1", "SKU-A") in res.withheld
    assert res.packet_flags["P1"] == ExceptionCode.RT_RTO_AMBIGUITY.value
    codes = {e.code for e in res.collector.records}
    assert ExceptionCode.RT_RTO_AMBIGUITY in codes


def test_split_packet_different_skus_not_flagged_as_ambiguous(fwd_sku, rt_sku, rto_sku):
    res = SkuReconciliation().reconcile(fwd_sku, rt_sku, rto_sku)
    assert res.packet_flags == {}   # a legitimate split never forces EXCEPTION


def test_clean_packet_with_no_returns(fwd_sku):
    res = SkuReconciliation().reconcile(fwd_sku, None, None)
    row = _by_sku(res).loc[("P002", "SKU-C")]
    assert row.returned_qty == 0
    assert row.flag is None
    assert res.withheld == set()
    assert res.packet_flags == {}


def test_missing_sku_columns_falls_back():
    fwd = pd.DataFrame({"packet_id": ["P1"], "shipment_value": [100]})
    res = SkuReconciliation().reconcile(fwd, None, None)
    assert res.frame.empty
    assert res.withheld is None      # signals "SKU stage did not run"
    codes = {e.code for e in res.collector.records}
    assert ExceptionCode.MISSING_SKU in codes
