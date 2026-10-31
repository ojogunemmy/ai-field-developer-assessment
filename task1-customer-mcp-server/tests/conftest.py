import pytest

from customer_mcp_server import refunds


@pytest.fixture(autouse=True)
def clear_refunds():
    refunds.PROCESSED_REFUNDS.clear()
    yield
    refunds.PROCESSED_REFUNDS.clear()


@pytest.fixture
def refund_spy(monkeypatch):
    """Record every call that reaches the refund operation."""
    calls = []
    real = refunds.process_refund

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(refunds, "process_refund", spy)
    return calls
