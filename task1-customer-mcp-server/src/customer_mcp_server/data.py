"""Mock customer records and lookup.

Stands in for a real customer database. Records are keyed by customer ID.
"""

import copy
import logging
from typing import Any

logger = logging.getLogger(__name__)

CUSTOMERS: dict[str, dict[str, Any]] = {
    "CUST-00001": {
        "customer_id": "CUST-00001",
        "name": "Ada Okafor",
        "email": "ada.okafor@example.com",
        "status": "active",
        "tier": "gold",
        "created_at": "2023-02-14",
        "total_spent": 4820.50,
    },
    "CUST-00002": {
        "customer_id": "CUST-00002",
        "name": "Liam Chen",
        "email": "liam.chen@example.com",
        "status": "active",
        "tier": "silver",
        "created_at": "2023-08-03",
        "total_spent": 1290.00,
    },
    "CUST-00003": {
        "customer_id": "CUST-00003",
        "name": "Sofia Martins",
        "email": "sofia.martins@example.com",
        "status": "suspended",
        "tier": "bronze",
        "created_at": "2024-01-22",
        "total_spent": 310.75,
    },
    "CUST-00004": {
        "customer_id": "CUST-00004",
        "name": "Noah Adeyemi",
        "email": "noah.adeyemi@example.com",
        "status": "active",
        "tier": "platinum",
        "created_at": "2022-11-09",
        "total_spent": 15630.20,
    },
    "CUST-00005": {
        "customer_id": "CUST-00005",
        "name": "Emma Schulz",
        "email": "emma.schulz@example.com",
        "status": "closed",
        "tier": "bronze",
        "created_at": "2024-06-30",
        "total_spent": 89.99,
    },
}


def get_customer(customer_id: str) -> dict[str, Any] | None:
    """Return a copy of the customer record, or None if it does not exist.

    A copy is returned so callers cannot mutate the mock store.
    """
    record = CUSTOMERS.get(customer_id)
    if record is None:
        logger.info("Customer not found: %s", customer_id)
        return None
    logger.debug("Customer found: %s", customer_id)
    return copy.deepcopy(record)


def customer_exists(customer_id: str) -> bool:
    return customer_id in CUSTOMERS
