import re
from datetime import datetime

from customer_mcp_server.refunds import (
    PROCESSED_REFUNDS,
    generate_refund_reference,
    process_refund,
)


def test_process_refund_returns_transaction_details():
    refund = process_refund("CUST-00001", 25.5, "Item arrived damaged")
    assert refund["status"] == "processed"
    assert refund["customer_id"] == "CUST-00001"
    assert refund["amount"] == 25.5
    assert refund["reason"] == "Item arrived damaged"
    assert re.fullmatch(r"RF-[0-9A-F]{12}", refund["refund_reference"])
    assert datetime.fromisoformat(refund["processed_at"]).tzinfo is not None


def test_process_refund_is_recorded():
    refund = process_refund("CUST-00001", 10, "Duplicate charge on card")
    assert PROCESSED_REFUNDS == [refund]


def test_refund_references_are_unique():
    references = {generate_refund_reference() for _ in range(1000)}
    assert len(references) == 1000
