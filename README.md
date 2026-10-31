# AI Field Developer / Solutions Engineer Assessment

Solutions to the four practical tasks, in Python. Each task is a standalone
project with its own README, tests and setup.

| Task | Folder | What it is | Tests |
|---|---|---|---|
| 1 | [`task1-customer-mcp-server/`](task1-customer-mcp-server/) | MCP server over STDIO with `get_customer_record` and `trigger_refund`, strict Pydantic validation and JSON-RPC error mapping | 151 |
| 2 | [`task2-mcp-security-gateway/`](task2-mcp-security-gateway/) | HTTP reverse proxy for MCP: Bearer-token roles, blocks `admin_*` tool calls with `-32001` | 110 |
| 3 | [`task3-llm-guardrail-gateway/`](task3-llm-guardrail-gateway/) | LLM gateway that streams completions while redacting emails, SSNs and card numbers in real time | 406 |
| 4 | [`task4-llm-fallback-router/`](task4-llm-fallback-router/) | Token-aware sliding-window rate limiter (SQLite on disk) with primary-to-secondary model failover | 68 |

**How the four fit together:** [docs/how-they-work-together.md](docs/how-they-work-together.md).

## Running a Task

Each task has its own virtual environment:

```bash
cd task2-mcp-security-gateway        # or any task folder
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest                               # run its tests
```

Then follow the **Quick Start** in that task's README.

## Requirements

- Python 3.11 or later
- Node.js, only for MCP Inspector (Task 1 manual testing)

## Quality Checks

Every task passes `pytest --cov`, `ruff check`, `ruff format --check` and
`mypy --strict`. [`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs
all of them on every push.

## Repository Layout

```
ai-field-developer-assessment/
├── README.md                          This file
├── docs/how-they-work-together.md     Architecture across the four tasks
├── .github/workflows/ci.yml           CI for all four tasks
├── task1-customer-mcp-server/
├── task2-mcp-security-gateway/
├── task3-llm-guardrail-gateway/
└── task4-llm-fallback-router/
```
