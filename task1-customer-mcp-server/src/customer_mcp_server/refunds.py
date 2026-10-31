"""Mock refund processing.

Simulates a payment provider: no money moves. Each processed refund is kept
in memory so tests can confirm whether the operation actually ran.
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

PROCESSED_REFUNDS: list[dict[str, Any]] = []


def generate_refund_reference() -> str:
    return f"RF-{uuid.uuid4().hex[:12].upper()}"


def process_refund(customer_id: str, amount: float, reason: str) -> dict[str, Any]:
    """Execute the mock refund and return its transaction details.

    Callers must validate input and confirm the customer exists first; this
    function trusts its arguments.
    """
    refund = {
        "refund_reference": generate_refund_reference(),
        "status": "processed",
        "customer_id": customer_id,
        "amount": amount,
        "reason": reason,
        "processed_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    PROCESSED_REFUNDS.append(refund)
    logger.info(
        "Refund %s processed for %s (amount=%s)",
        refund["refund_reference"],
        customer_id,
        amount,
    )
    return refund
