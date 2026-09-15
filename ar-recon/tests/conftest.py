from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def fwd():
    return pd.DataFrame({
        "packet_id": ["P001", "P001", "P002", "P003", "P004"],
        "shipment_value": [500, 300, 500, 700, 900],   # P001 has two lines -> 800
    })


@pytest.fixture
def rt():
    return pd.DataFrame({"packet_id": ["P001"], "shipment_value": [800]})


@pytest.fixture
def rto():
    return pd.DataFrame({"packet_id": ["P003"], "shipment_value": [200]})


@pytest.fixture
def settle_fwd():
    return pd.DataFrame({
        "order_release_id": ["OR001", "OR002", "OR003", "OR003"],  # OR003 duplicated -> aggregated
        "customer_paid_amt": [1000, 500, 400, 300],
    })


@pytest.fixture
def settle_rev():
    return pd.DataFrame({
        "order_release_id": ["OR001", "OR003"],
        "customer_paid_amt": [200, 100],
    })


@pytest.fixture
def tmp_xlsx(tmp_path):
    p = tmp_path / "sales.xlsx"
    pd.DataFrame({"Packet ID": ["P1"], "Shipment Value": [10]}).to_excel(p, index=False)
    return p
