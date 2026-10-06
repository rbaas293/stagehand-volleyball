# Python scraper

See the [root README](../README.md) for config, modes, and environment variables.

## Requirements

- **Python 3.11+** (required by the `stagehand` package; 3.12/3.13 fine)
- **Google Chrome** only when using **llm** mode (local browser). Lean mode needs neither Chrome nor an LLM key.
- `XAI_API_KEY` for **llm** mode (local), **or** `BROWSERBASE_API_KEY`

## Setup

```bash
cd python
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # or reuse the repo-root .env
# edit ../config.yaml — captainName / levels / mode / …
export XAI_API_KEY=...             # llm mode only, if not using .env
python main.py                     # lean by default
```

`main.py` reads both `python/.env` and the repo-root `../.env`. Precedence, highest first: **shell exports > `python/.env` > `../.env`**.

Config: prefers **`../config.yaml`**, falls back to `python/config.yaml`.
Output: `python/games.json` (gitignored).
