"""Exact column names for each dataset table, transcribed from the header rows shared.

Every dataset column is stored as TEXT (exactly like the source SQL, which CASTs at query time).
Two housekeeping columns are added to every table by the loader:
    _row_no       -> row number in the uploaded file (header = row 1), so every exception points to a real row
    _source_file  -> name of the uploaded file

If an uploaded file contains a header that is not listed here, the loader ADDS it to the table
(ALTER TABLE ... ADD COLUMN ... TEXT) instead of failing, and reports it on the upload screen.
This is needed because the settlement files carry month-specific columns (Settlement_on_2024_02_02 ...).
"""

DATASETS = {
    "sales_forward": {
        "label": "Sales - Forward",
        "key": "packet_id",
        "amount": "shipment_value",
        "columns": [
            "Shipping_case", "state", "store_id", "packet_id", "New_Parent_id", "order_id", "item_id", "sku_id",
            "pps_item_id", "order_status", "item_status", "order_created_date", "order_packed_date",
            "order_shipped_date", "restocked_date", "order_rto_date", "gender", "seller_id", "seller_name",
            "seller_type", "master_category", "brand", "article_type", "payment_method", "tax_rate", "igst_rate",
            "cgst_rate", "sgst_rate", "pincode", "is_try_and_buy", "Customer_Delivery_State_Code", "warehouse_id",
            "warehouse_name", "address", "location", "igst_amt", "cgst_amt", "sgst_amt", "tax_amount", "quantity",
            "loyalty_credit", "loyalty_Amount", "InvoiceAmount", "shipment_value", "platform_charges", "MRP",
            "shipping_charges", "gift_charges", "Discount", "cashback", "Tax_user_rec", "GiftCard", "wallet",
            "Vat_refund", "Mynts_Used", "Instant_Cashback", "Value_Shipping", "TCS", "Net_Amount",
            "certificateNumber", "Seller_GSTIN", "starts_from", "ends_on", "Tax_Seller_GSTIN", "base_amount",
            "tds_rate", "tcs_amount", "tds_amount", "cgst_tcs_amount", "sgst_tcs_amount", "igst_tcs_amount",
        ],
    },
    "sales_rt": {
        "label": "Sales - RT (customer return)",
        "key": "packet_id",
        "amount": "shipment_value",
        "columns": [
            "store_id", "seller_id", "seller_type", "packet_id", "item_id", "sku_id", "core_item_id", "Qty",
            "item_status", "order_status", "shipment_id", "is_realised", "is_delivered", "fr_is_refunded",
            "order_delivered_date", "return_status", "is_shipped", "article_type", "master_category", "brand",
            "gender", "tax", "tax_rate", "igst_rate", "payment_method", "sgst_rate", "location_type",
            "order_created_date", "order_packed_date", "cgst_rate", "fr_refunded_date", "fr_restocked_date",
            "return_status_display_name", "order_shipped_date", "Delivery_State", "Seller_state_Code",
            "warehouse_id", "warehouse_name", "last_modified_on", "warehouse_pincode", "customer_pincode",
            "exchange_ID", "refund_mode", "warehouse_city", "lmc_warehouse_id", "foci_warehouse_id", "state",
            "dm_warehouse_pincode", "shipping_charges", "row_num", "MRP", "shipment_value",
            "item_revenue_inc_cashback", "Instant_Cashback", "gift_charges", "GiftCard", "phonepe",
            "loyalty_pts_used", "cashback", "certificateNumber", "myntra_credits", "Mynts", "fr_refund_amount",
            "TCS", "seller_name", "Net_Amount", "base_amount", "Seller_GSTIN", "starts_from", "ends_on",
            "Shipping_case", "Tax_Seller_GSTIN", "tds_rate", "tcs_amount", "tds_amount", "cgst_tcs_amount",
            "sgst_tcs_amount", "igst_tcs_amount",
        ],
    },
    "sales_rto": {
        "label": "Sales - RTO (return to origin)",
        "key": "packet_id",
        "amount": "shipment_value",
        "columns": [
            "Shipping_case", "state", "old_Parent_id", "New_Parent_id", "order_id", "packet_id", "item_id",
            "sku_id", "pps_item_id", "order_status", "item_status", "order_created_date", "order_packed_date",
            "order_shipped_date", "restocked_date", "order_rto_date", "order_cancel_date", "courier_code",
            "gender", "core_item_id", "seller_id", "seller_type", "master_category", "brand", "brand_type",
            "commercial_type", "article_type", "payment_method", "tax_rate", "igst_rate", "cgst_rate",
            "sgst_rate", "pincode", "is_try_and_buy", "originalorder", "warehouse_id", "warehouse_name",
            "address", "location", "is_rto", "is_realised", "is_shipped", "is_delivered", "customer_state",
            "customer_pincode", "customer_city", "foci_warehouse_id", "warehouse_state", "dm_warehouse_pincode",
            "warehouse_city", "lmc_warehouse_id", "dup_flag", "igst_amt", "cgst_amt", "sgst_amt", "tax_amount",
            "quantity", "igst_tax_amount", "cgst_tax_amount", "sgst_tax_amount", "MRP", "shipment_value",
            "platform_charges", "item_revenue_inc_cashback", "shipping_charges", "gift_charges",
            "refund_amount", "wallet_redeemed", "cashback", "referral_points_used", "gift_card_amount",
            "myntra_credits", "Instant_Cashback", "Mynts", "TCS", "seller_name", "Net_Amount",
            "certificateNumber", "Seller_GSTIN", "starts_from", "ends_on", "order_delivered_date",
            "Tax_Seller_GSTIN", "base_amount", "tds_rate", "tcs_amount", "tds_amount", "cgst_tcs_amount",
            "sgst_tcs_amount", "igst_tcs_amount",
        ],
    },
}

# Settlement forward and reverse share the same layout (reverse additionally has return_id).
_SETTLEMENT_COLUMNS = [
    "Platform", "order_release_id", "order_line_id", "return_type", "return_date", "packing_date",
    "delivery_date", "ecommerce_portal_name", "sku_code", "invoice_number", "packet_id", "hsn_code",
    "product_tax_category", "currency", "customer_paid_amount", "postpaid_amount", "prepaid_amount", "mrp",
    "total_discount_amount", "shipping_case", "total_tax_rate", "igst_amount", "cgst_amount", "sgst_amount",
    "tcs_amount", "tds_amount", "commission_percentage", "minimum_commission", "platform_fees",
    "total_commission", "total_commission_plus_tcs_tds_deduction", "total_logistics_deduction",
    "shipping_fee", "fixed_fee", "pick_and_pack_fee", "payment_gateway_fee", "total_tax_on_logistics",
    "article_level", "shipment_zone_classification", "customer_paid_amt", "total_expected_settlement",
    "total_actual_settlement", "amount_pending_settlement", "prepaid_commission_deduction",
    "prepaid_logistics_deduction", "prepaid_payment", "postpaid_commission_deduction",
    "postpaid_logistics_deduction", "postpaid_payment", "settlement_date_prepaid_comm_deduction",
    "settlement_date_prepaid_logistics_deduction", "settlement_date_prepaid_payment",
    "settlement_date_postpaid_comm_deduction", "settlement_date_postpaid_logistics_deduction",
    "settlement_date_postpaid_payment", "bank_utr_no_prepaid_comm_deduction",
    "bank_utr_no_prepaid_logistics_deduction", "bank_utr_no_prepaid_payment",
    "bank_utr_no_postpaid_comm_deduction", "bank_utr_no_postpaid_logistics_deduction",
    "bank_utr_no_postpaid_payment",
    # Settlement_on_YYYY_MM_DD / SettlementOnNN columns are month-specific and are added automatically at load time.
    "postpaid_amount_other", "prepaid_amount_other", "shipping_amount", "gift_amount", "additional_amount",
    "cess_amount", "taxable_amount", "igst_rate", "cgst_rate", "sgst_rate", "cess_rate", "tcs_igst_rate",
    "tcs_sgst_rate", "tcs_cgst_rate", "tds_rate", "brand", "gender", "brand_type", "article_type",
    "supply_type", "try_and_buy_purchase", "customer_name", "customer_delivery_pin_code", "seller_gstn",
    "seller_name", "myntra_gstn", "shipping_city", "shipping_pin_code", "shipping_state",
    "shipping_state_code", "prepaid_commission_percentage", "prepaid_minimum_commission",
    "prepaid_platform_fees", "prepaid_total_commission", "prepaid_ship_commission_charge",
    "prepaid_gift_commission_charge", "prepaid_cod_commission_charge", "prepaid_cart_discount",
    "prepaid_coupon_discount", "postpaid_commission_percentage", "postpaid_minimum_commission",
    "postpaid_platform_fees", "postpaid_total_commission", "postpaid_ship_commission_charge",
    "postpaid_gift_commission_charge", "postpaid_cod_commission_charge", "postpaid_cart_discount",
    "postpaid_coupon_discount", "seller_order_id", "tcs_amount_prepaid", "tcs_amount_postpaid",
    "tds_amount_prepaid", "tds_amount_postpaid", "seller_tier",
]

DATASETS["settlement_forward"] = {
    "label": "Settlement - Forward (platform)",
    "key": "order_release_id",
    "amount": "customer_paid_amount",
    "columns": list(_SETTLEMENT_COLUMNS),
}
DATASETS["settlement_reverse"] = {
    "label": "Settlement - Reverse (platform)",
    "key": "order_release_id",
    "amount": "customer_paid_amount",
    "columns": _SETTLEMENT_COLUMNS[:-6] + ["return_id"] + _SETTLEMENT_COLUMNS[-6:],
}

# Columns whose values are identifiers: trailing ".0" produced by Excel numeric cells is stripped on load
ID_COLUMNS = {"packet_id", "order_release_id", "order_id", "item_id", "sku_id", "order_line_id", "return_id",
              "New_Parent_id", "old_Parent_id", "shipment_id", "invoice_number", "seller_order_id"}

DATASET_ORDER = ["sales_forward", "sales_rt", "sales_rto", "settlement_forward", "settlement_reverse"]
