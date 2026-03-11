# ThinkTrace Inspection Bot

Telegram inspection bot for ThinkTrace integration.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt  # If requirements.txt exists
```

## Running

```bash
python main.py
```

## Structure

- `main.py` - Bot entry point
- `worker.py` - Worker processes
- `session_store.py` - Session management
- `rewrite_engine.py` - Text processing engine
- `template_word_builder.py` - Template builder
- `word_builder.py` - Word document builder
- `export_builder.py` - Export utilities
