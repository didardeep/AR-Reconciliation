import pandas as pd
import pytest
from app.core.enums import FileType
from app.ingestion.excel_reader import read_excel, read_headers, ExcelIngestError
from app.ingestion.column_mapper import map_columns


def test_header_aliases_map_to_canonical():
    m = map_columns(["Order Release ID", "CUSTOMER_PAID_AMOUNT"], FileType.SETTLEMENT_FORWARD)
    assert set(m.values()) == {"order_release_id", "customer_paid_amt"}


def test_read_excel_maps_and_preserves(tmp_xlsx):
    df = read_excel(tmp_xlsx, FileType.SALES_FORWARD)
    assert "packet_id" in df.columns
    assert "shipment_value" in df.columns


def test_no_duplicate_canonical_columns(tmp_path):
    p = tmp_path / "dup.xlsx"
    pd.DataFrame({"customer_paid_amount": [1], "taxable_amount": [2],
                  "packet_id": ["P1"]}).to_excel(p, index=False)
    df = read_excel(p, FileType.SALES_FORWARD)
    assert list(df.columns).count("shipment_value") == 1


def test_missing_file_raises():
    with pytest.raises(ExcelIngestError):
        read_excel("/nonexistent/file.xlsx", FileType.SALES_FORWARD)
