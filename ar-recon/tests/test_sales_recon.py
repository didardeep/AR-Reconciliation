import pandas as pd
from app.core.enums import ExceptionCode, ReverseType
from app.reconciliation.sales import SalesReconciliation


def _by_packet(res):
    return res.frame.set_index("packet_id")


def test_forward_only(fwd):
    res = SalesReconciliation().reconcile(fwd, None, None)
    row = _by_packet(res).loc["P002"]
    assert row.forward_shipment_value == 500
    assert row.reverse_type == ReverseType.NONE.value
    assert row.net_sales == 500


def test_forward_plus_rt(fwd, rt):
    res = SalesReconciliation().reconcile(fwd, rt, None)
    row = _by_packet(res).loc["P001"]
    assert row.forward_shipment_value == 800   # two lines aggregated
    assert row.reverse_type == ReverseType.RT.value
    assert row.net_sales == 0


def test_forward_plus_rto(fwd, rto):
    res = SalesReconciliation().reconcile(fwd, None, rto)
    row = _by_packet(res).loc["P003"]
    assert row.reverse_type == ReverseType.RTO.value
    assert row.reverse_value == 200
    assert row.net_sales == 500


def test_does_not_subtract_both(fwd, rt, rto):
    # P001 only in RT, P003 only in RTO -> each uses exactly one reverse source
    res = SalesReconciliation().reconcile(fwd, rt, rto)
    d = _by_packet(res)
    assert d.loc["P001"].reverse_type == ReverseType.RT.value
    assert d.loc["P003"].reverse_type == ReverseType.RTO.value


def test_packet_in_both_rt_and_rto_is_ambiguous(fwd):
    rt = pd.DataFrame({"packet_id": ["P002"], "shipment_value": [100]})
    rto = pd.DataFrame({"packet_id": ["P002"], "shipment_value": [150]})
    res = SalesReconciliation().reconcile(fwd, rt, rto)
    row = _by_packet(res).loc["P002"]
    assert row.reverse_type == ReverseType.AMBIGUOUS.value
    assert row.reverse_value == 0            # never double-counted
    codes = {e.code for e in res.collector.records}
    assert ExceptionCode.RT_RTO_AMBIGUITY in codes


def test_rt_not_in_forward(fwd):
    rt = pd.DataFrame({"packet_id": ["PZZZ"], "shipment_value": [50]})
    res = SalesReconciliation().reconcile(fwd, rt, None)
    assert ExceptionCode.RT_NOT_IN_FORWARD in {e.code for e in res.collector.records}


def test_rto_not_in_forward(fwd):
    rto = pd.DataFrame({"packet_id": ["PZZZ"], "shipment_value": [50]})
    res = SalesReconciliation().reconcile(fwd, None, rto)
    assert ExceptionCode.RTO_NOT_IN_FORWARD in {e.code for e in res.collector.records}


def test_duplicate_packet_flagged(fwd):
    res = SalesReconciliation().reconcile(fwd, None, None)
    assert ExceptionCode.DUPLICATE_PACKET_ID in {e.code for e in res.collector.records}


def test_missing_shipment_value():
    fwd = pd.DataFrame({"packet_id": ["P1"], "shipment_value": [None]})
    res = SalesReconciliation().reconcile(fwd, None, None)
    assert ExceptionCode.MISSING_SHIPMENT_VALUE in {e.code for e in res.collector.records}


def test_reverse_gt_forward():
    fwd = pd.DataFrame({"packet_id": ["P1"], "shipment_value": [100]})
    rt = pd.DataFrame({"packet_id": ["P1"], "shipment_value": [300]})
    res = SalesReconciliation().reconcile(fwd, rt, None)
    assert ExceptionCode.REVERSE_GT_FORWARD in {e.code for e in res.collector.records}


def test_indicator_column_decides(fwd):
    # both files carry the packet, but an explicit indicator says RT
    rt = pd.DataFrame({"packet_id": ["P002"], "shipment_value": [100], "return_type": ["RT"]})
    res = SalesReconciliation().reconcile(fwd, rt, None)
    assert _by_packet(res).loc["P002"].reverse_type == ReverseType.RT.value
