import logging
import subprocess
import sys

from customer_mcp_server.logging_config import configure_logging


def test_logs_go_to_stderr_not_stdout(capsys):
    configure_logging("DEBUG")
    logging.getLogger("test").info("hello from test")

    captured = capsys.readouterr()
    assert "hello from test" in captured.err
    assert captured.out == ""


def test_log_level_from_env(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "warning")
    configure_logging()
    assert logging.getLogger().level == logging.WARNING


def test_entrypoint_writes_nothing_to_stdout():
    # Empty stdin: the server starts, sees EOF and shuts down without
    # receiving any request, so stdout must stay empty.
    result = subprocess.run(
        [sys.executable, "-m", "customer_mcp_server"],
        input="",
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert "Starting customer-mcp-server" in result.stderr
