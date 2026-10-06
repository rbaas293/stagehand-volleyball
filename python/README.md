# Python scraper

See the [root README](../README.md) for config, modes, and environment variables.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

Config: prefers **`../config.yaml`**, falls back to `python/config.yaml`.
Env: shell > `python/.env` > `../.env`.
Output: `python/games.json` (gitignored).
