import math

import pytest
from pydantic import ValidationError

from customer_mcp_server.data import CUSTOMERS
from customer_mcp_server.models import CustomerLookupInput, RefundInput

VALID_REFUND = {
    "customer_id": "CUST-00001",
    "amount": 25.50,
    "reason": "Item arrived damaged",
}


def refund(**overrides):
    return RefundInput.model_validate({**VALID_REFUND, **overrides})


# --- customer ID ---------------------------------------------------------


def test_valid_customer_id():
    assert CustomerLookupInput(customer_id="CUST-00001").customer_id == "CUST-00001"


def test_all_mock_ids_are_valid():
    for customer_id in CUSTOMERS:
        CustomerLookupInput(customer_id=customer_id)


@pytest.mark.parametrize(
    "customer_id",
    [
        "",
        "CUST-",
        "CUST-1234",
        "CUST-123456",
        "CUST-ABCDE",
        "cust-00001",
        "CUST00001",
        "CUST_00001",
        " CUST-00001",
        "CUST-00001 ",
        "CUST-00001\n",
        "XCUST-00001",
        "CUST-١٢٣٤٥",
    ],
)
def test_invalid_customer_id_format(customer_id):
    with pytest.raises(ValidationError):
        CustomerLookupInput(customer_id=customer_id)


@pytest.mark.parametrize("customer_id", [1, 12345, None, True, ["CUST-00001"]])
def test_non_string_customer_id(customer_id):
    with pytest.raises(ValidationError):
        CustomerLookupInput.model_validate({"customer_id": customer_id})


def test_lookup_missing_customer_id():
    with pytest.raises(ValidationError):
        CustomerLookupInput.model_validate({})


def test_lookup_rejects_unexpected_fields():
    with pytest.raises(ValidationError, match="extra"):
        CustomerLookupInput.model_validate({"customer_id": "CUST-00001", "admin": True})


# --- refund amount -------------------------------------------------------


def test_valid_refund():
    data = refund()
    assert data.customer_id == "CUST-00001"
    assert data.amount == 25.50
    assert data.reason == "Item arrived damaged"


@pytest.mark.parametrize("amount", [0.01, 0.001, 1, 19.999, 100, 50_000, 1e9, 0.1 + 0.2, 1e-9])
def test_valid_amounts(amount):
    assert refund(amount=amount).amount == amount


@pytest.mark.parametrize(
    "amount",
    [0, 0.0, -0.0, -1, -25.5, math.inf, -math.inf, math.nan],
)
def test_invalid_amounts(amount):
    with pytest.raises(ValidationError):
        refund(amount=amount)


@pytest.mark.parametrize("amount", ["25.50", "abc", "", None, True, [25], {"v": 25}])
def test_non_numeric_amounts(amount):
    with pytest.raises(ValidationError):
        refund(amount=amount)


# --- refund reason -------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    ["", "short", "123456789", "          ", "a b c d e f g h i", "\t\n  abc  \n"],
)
def test_short_reasons(reason):
    with pytest.raises(ValidationError, match="non-whitespace"):
        refund(reason=reason)


def test_reason_with_exactly_ten_characters():
    assert refund(reason="0123456789").reason == "0123456789"


def test_reason_counts_only_non_whitespace():
    # 10 non-whitespace characters spread across spaces.
    assert refund(reason="a b c d e f g h i j").reason == "a b c d e f g h i j"


def test_reason_is_stripped():
    assert refund(reason="  Item arrived damaged  ").reason == "Item arrived damaged"


@pytest.mark.parametrize("reason", [None, 1234567890, ["Item arrived damaged"]])
def test_non_string_reasons(reason):
    with pytest.raises(ValidationError):
        refund(reason=reason)


# --- missing and unexpected fields ---------------------------------------


@pytest.mark.parametrize("field", ["customer_id", "amount", "reason"])
def test_refund_missing_field(field):
    data = {k: v for k, v in VALID_REFUND.items() if k != field}
    with pytest.raises(ValidationError):
        RefundInput.model_validate(data)


def test_refund_rejects_unexpected_fields():
    with pytest.raises(ValidationError, match="extra"):
        refund(currency="USD")


def test_refund_invalid_customer_id():
    with pytest.raises(ValidationError):
        refund(customer_id="CUST-1")
