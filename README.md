# Document Routing Bot

Telegram service for routing incoming documents through configured accounts and processing channels.

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

On the first run the service creates a local `config.json`. Account settings, sessions, documents and logs stay outside Git.
