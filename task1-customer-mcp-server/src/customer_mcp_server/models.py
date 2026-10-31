"""Pydantic schemas for the MCP tools.

Input models forbid unexpected fields and use strict types, so values such as
the string "10" or the boolean true are rejected rather than coerced. Output
models define the ``structuredContent`` each tool returns and are published as
the tools' ``outputSchema``.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

# [0-9] rather than \d, which also matches non-ASCII digits such as U+0661.
CUSTOMER_ID_PATTERN = r"^CUST-[0-9]{5}$"
MIN_REASON_CHARS = 10

CustomerId = Annotated[
    str,
    StringConstraints(strict=True, pattern=CUSTOMER_ID_PATTERN),
    Field(description="Customer ID in the format CUST-XXXXX, where X is a digit."),
]


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CustomerLookupInput(StrictInput):
    customer_id: CustomerId


class RefundInput(StrictInput):
    customer_id: CustomerId
    amount: float = Field(
        gt=0,
        allow_inf_nan=False,
        description="Refund amount. Must be a finite number greater than zero.",
    )
    reason: str = Field(
        description=(
            f"Reason for the refund, at least {MIN_REASON_CHARS} non-whitespace characters."
        ),
    )

    @field_validator("reason")
    @classmethod
    def reason_has_enough_content(cls, value: str) -> str:
        non_whitespace = sum(1 for ch in value if not ch.isspace())
        if non_whitespace < MIN_REASON_CHARS:
            raise ValueError(
                f"reason must contain at least {MIN_REASON_CHARS} non-whitespace characters"
            )
        return value.strip()


class CustomerRecord(BaseModel):
    customer_id: str
    name: str
    email: str
    status: Literal["active", "suspended", "closed"]
    tier: Literal["bronze", "silver", "gold", "platinum"]
    created_at: str = Field(description="Account creation date, ISO 8601 (YYYY-MM-DD).")
    total_spent: float


class RefundResult(BaseModel):
    refund_reference: str = Field(description="Unique reference, format RF-XXXXXXXXXXXX.")
    status: Literal["processed"]
    customer_id: str
    amount: float
    reason: str
    processed_at: str = Field(description="UTC timestamp, ISO 8601.")
