"""MCP tool implementations.

Each tool takes the raw ``arguments`` dict from a ``tools/call`` request,
validates it with a strict Pydantic model, and only then runs the business
operation. Results are passed through the output models so they always match
the published ``outputSchema``.

Two kinds of failure are raised, and ``server.py`` maps them differently:

- ``InvalidArguments``: the arguments break the input schema. Reported as a
  JSON-RPC ``-32602`` (Invalid params) error.
- ``ToolError``: the arguments are valid but the operation cannot complete,
  such as an unknown customer. Reported as a tool result with ``isError``.
"""

import logging
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from customer_mcp_server import data, refunds
from customer_mcp_server.models import (
    CustomerLookupInput,
    CustomerRecord,
    RefundInput,
    RefundResult,
)

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)


class ToolError(Exception):
    """A tool failure reported to the client as an ``isError`` result."""


class InvalidArguments(Exception):
    """Arguments that fail schema validation, reported as JSON-RPC -32602.

    ``problems`` lists one ``{"field": ..., "message": ...}`` entry per failing
    field and is sent to the client as the error's ``data``.
    """

    def __init__(self, problems: list[dict[str, str]]) -> None:
        self.problems = problems
        details = "; ".join(f"{p['field']}: {p['message']}" for p in problems)
        super().__init__(f"Invalid params: {details}")


def validation_problems(exc: ValidationError) -> list[dict[str, str]]:
    """Describe each failing field without echoing the rejected values."""
    problems = []
    for err in exc.errors():
        field = ".".join(str(part) for part in err["loc"]) or "arguments"
        message = err["msg"].removeprefix("Value error, ")
        if err["type"] == "extra_forbidden":
            message = "unexpected field"
        problems.append({"field": field, "message": message})
    return problems


def _validate(model: type[ModelT], arguments: dict[str, Any]) -> ModelT:
    try:
        return model.model_validate(arguments)
    except ValidationError as exc:
        raise InvalidArguments(validation_problems(exc)) from exc


def get_customer_record(arguments: dict[str, Any]) -> dict[str, Any]:
    params = _validate(CustomerLookupInput, arguments)
    record = data.get_customer(params.customer_id)
    if record is None:
        raise ToolError(f"Customer not found: {params.customer_id}")
    return CustomerRecord.model_validate(record).model_dump()


def trigger_refund(arguments: dict[str, Any]) -> dict[str, Any]:
    params = _validate(RefundInput, arguments)
    if not data.customer_exists(params.customer_id):
        raise ToolError(f"Customer not found: {params.customer_id}")
    refund = refunds.process_refund(params.customer_id, params.amount, params.reason)
    return RefundResult.model_validate(refund).model_dump()
