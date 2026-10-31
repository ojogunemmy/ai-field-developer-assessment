import math

import pytest

from customer_mcp_server.models import CustomerRecord, RefundResult
from customer_mcp_server.refunds import PROCESSED_REFUNDS
from customer_mcp_server.tools import (
    InvalidArguments,
    ToolError,
    get_customer_record,
    trigger_refund,
)

VALID_REFUND = {
    "customer_id": "CUST-00002",
    "amount": 49.99,
    "reason": "Order cancelled before shipping",
}


def fields(exc_info):
    return [problem["field"] for problem in exc_info.value.problems]


# --- get_customer_record -------------------------------------------------


def test_lookup_returns_record():
    record = get_customer_record({"customer_id": "CUST-00004"})
    assert record["customer_id"] == "CUST-00004"
    assert record["name"] == "Noah Adeyemi"


def test_lookup_returns_inactive_customers_too():
    assert get_customer_record({"customer_id": "CUST-00003"})["status"] == "suspended"


def test_lookup_nonexistent_customer():
    with pytest.raises(ToolError, match="Customer not found: CUST-99999"):
        get_customer_record({"customer_id": "CUST-99999"})


@pytest.mark.parametrize("customer_id", ["CUST-1", "cust-00001", "", 12345, None])
def test_lookup_invalid_id(customer_id):
    with pytest.raises(InvalidArguments) as exc_info:
        get_customer_record({"customer_id": customer_id})
    assert fields(exc_info) == ["customer_id"]


def test_lookup_missing_id():
    with pytest.raises(InvalidArguments) as exc_info:
        get_customer_record({})
    assert exc_info.value.problems == [{"field": "customer_id", "message": "Field required"}]


def test_lookup_unexpected_field():
    with pytest.raises(InvalidArguments) as exc_info:
        get_customer_record({"customer_id": "CUST-00001", "include_deleted": True})
    assert exc_info.value.problems == [{"field": "include_deleted", "message": "unexpected field"}]


# --- trigger_refund ------------------------------------------------------


def test_refund_success(refund_spy):
    result = trigger_refund(VALID_REFUND)
    assert result["status"] == "processed"
    assert result["customer_id"] == "CUST-00002"
    assert result["amount"] == 49.99
    assert result["refund_reference"].startswith("RF-")
    assert len(refund_spy) == 1
    assert PROCESSED_REFUNDS == [result]


@pytest.mark.parametrize("amount", [0.001, 12.345, 50_000, 1e9])
def test_refund_accepts_any_positive_float(refund_spy, amount):
    assert trigger_refund({**VALID_REFUND, "amount": amount})["amount"] == amount


def test_refund_reason_is_stripped(refund_spy):
    result = trigger_refund({**VALID_REFUND, "reason": "   Order cancelled   "})
    assert result["reason"] == "Order cancelled"


def test_each_refund_gets_unique_reference():
    first = trigger_refund(VALID_REFUND)
    second = trigger_refund(VALID_REFUND)
    assert first["refund_reference"] != second["refund_reference"]


def test_refund_nonexistent_customer_is_not_processed(refund_spy):
    with pytest.raises(ToolError, match="Customer not found: CUST-99999"):
        trigger_refund({**VALID_REFUND, "customer_id": "CUST-99999"})
    assert refund_spy == []


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"customer_id": "CUST-ABC"}, "customer_id"),
        ({"customer_id": 2}, "customer_id"),
        ({"amount": 0}, "amount"),
        ({"amount": -10}, "amount"),
        ({"amount": math.inf}, "amount"),
        ({"amount": -math.inf}, "amount"),
        ({"amount": math.nan}, "amount"),
        ({"amount": "49.99"}, "amount"),
        ({"amount": "lots"}, "amount"),
        ({"amount": True}, "amount"),
        ({"amount": None}, "amount"),
        ({"reason": "too short"}, "reason"),
        ({"reason": "          "}, "reason"),
        ({"reason": 1234567890}, "reason"),
        ({"currency": "USD"}, "currency"),
    ],
)
def test_invalid_refund_never_reaches_refund_operation(refund_spy, overrides, field):
    with pytest.raises(InvalidArguments) as exc_info:
        trigger_refund({**VALID_REFUND, **overrides})
    assert fields(exc_info) == [field]
    assert refund_spy == []
    assert PROCESSED_REFUNDS == []


def test_every_failing_field_is_reported(refund_spy):
    with pytest.raises(InvalidArguments) as exc_info:
        trigger_refund({"customer_id": "bad", "amount": 0, "reason": "short", "x": 1})
    assert fields(exc_info) == ["customer_id", "amount", "reason", "x"]
    assert refund_spy == []


@pytest.mark.parametrize("missing", ["customer_id", "amount", "reason"])
def test_refund_missing_field_is_not_processed(refund_spy, missing):
    arguments = {k: v for k, v in VALID_REFUND.items() if k != missing}
    with pytest.raises(InvalidArguments) as exc_info:
        trigger_refund(arguments)
    assert exc_info.value.problems == [{"field": missing, "message": "Field required"}]
    assert refund_spy == []


def test_error_message_does_not_echo_rejected_values():
    with pytest.raises(InvalidArguments) as exc_info:
        trigger_refund({**VALID_REFUND, "amount": "SECRET-VALUE"})
    assert "SECRET-VALUE" not in str(exc_info.value)
    assert "SECRET-VALUE" not in str(exc_info.value.problems)


def test_results_match_output_models():
    CustomerRecord.model_validate(get_customer_record({"customer_id": "CUST-00001"}))
    RefundResult.model_validate(trigger_refund(VALID_REFUND))
