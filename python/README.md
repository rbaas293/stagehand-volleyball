# Python scraper

See the [root README](../README.md) for config, modes, and environment variables.

## Requirements

- **Python 3.11+** (required by the `stagehand` package; 3.12/3.13 fine)
- **Google Chrome** only when using **llm** mode (local browser). Lean mode needs neither Chrome nor an LLM key.
- `XAI_API_KEY` for **llm** mode (local), **or** `BROWSERBASE_API_KEY`

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env               # or reuse the repo-root .env
cp ../config.example.yaml ../config.yaml   # once; gitignored
# edit ../config.yaml — captainName / levels / mode / …
export XAI_API_KEY=...             # llm mode only, if not using .env
python main.py                     # lean by default
```

Config: prefers **`../config.yaml`**, falls back to `python/config.yaml`.
Env: shell > `python/.env` > `../.env`.
Output: `python/games.json` (gitignored).

## Packaging (wheels / shiv)

`pyproject.toml` packages `main.py`, `lean_api.py`, and `token_usage.py` with
console script `stagehand-volleyball` (`main:main`). Dependencies stay in sync
with `requirements.txt`.

```bash
cd python
pip install -e ".[dev]"          # editable + ruff/pytest/build/shiv
python -m build                  # wheels + sdist under dist/
shiv -c stagehand-volleyball -o dist/shiv/stagehand-volleyball.pyz .
pytest -q                        # unit + smoke (no live network)
```

GitHub Actions (`.github/workflows/python-ci.yml`) gates on scraper PR #1 being
merged, then runs checks (3.11/3.12), wheel builds, and shiv binaries.

