# Contributing

Follow the phased roadmap and acceptance criteria in [AGENTS.md](AGENTS.md). Use Python 3.12 and install with `uv sync --locked`.

Run `uv run --locked ruff check`, `uv run --locked ruff format --check`, `uv run --locked mypy`, and `uv run --locked pytest` before proposing changes. Use synthetic fixtures for credentials and chain data. Document user-facing changes in the changelog.
