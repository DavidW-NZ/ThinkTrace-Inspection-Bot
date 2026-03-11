# Inspection Setup Integration Discovery

**Task:** Discovery and documentation of Telegram bot integration points for future read-only Inspection Setup retrieval.

**Branch:** `feature/discover-inspection-setup-integration-points`
**Date:** 2026-03-11

---

## Discovery Summary

### 1. Telegram Bot Entry Point

**File:** `main.py`

**Application Builder Pattern:**
```python
def build_app():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN environment variable.")

    app = ApplicationBuilder().token(token).post_init(post_init).build()

    # Command handlers registered
    app.add_handler(CommandHandler("start", cmd_start))
    # ... (other handlers)

    # Message handlers
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, on_document_image))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    return app
```

**Main Entry:**
- Function: `main()`
- Behavior:
  1. Change working directory to `DATA_ROOT`
  2. Ensure session directories exist
  3. Build app via `build_app()`
  4. Run long polling

**Key Insight:** The bot uses `python-telegram-bot` (v20+) with `ApplicationBuilder` pattern and `MessageHandler` for non-command text input.

---

### 2. Command / Handler Structure

**Commands Registered:**
- `/start` - Project selection flow
- `/new` - Add new inspection observation
- `/action_required` - Add Action Required item
- `/action_completed` - Add Action Completed item
- `/goto` - Select item for CAPTURING auto-append
- `/done` - Transition to REVIEW status
- `/review` - Show session summary
- `/info` - Edit inspection header info
- `/edit` - Edit selected item
- `/add` - Add review-only item
- `/hide` - Toggle observation include_in_report
- `/confirm` - Submit/lock for export
- `/cancel` - Cancel current mode

**Handler Pattern:**
- All handlers follow signature: `async def cmd_<name>(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None`
- Each handler loads session via `session_store.load_session_for_chat(chat_id)`
- Status checks are performed before allowing actions (e.g., `status_is_locked()`, `status_is_capturing()`)

**Key Insight:** Commands are organized as mode-first flows with `set_mode()`/`clear_mode()` helpers to manage multi-step interactions.

---

### 3. Config / Environment Loading Pattern

**Environment Variables:**
- `TELEGRAM_BOT_TOKEN` (required) - Bot token from Telegram BotFather
- `INSPECTION_DATA_ROOT` (optional, fallback: `Path.cwd()`) - Root for all persistent data

**Config Loading Function:**
```python
def get_data_root() -> Path:
    root = (os.environ.get("INSPECTION_DATA_ROOT") or "").strip()
    if root:
        return Path(root).expanduser().resolve()
    return Path.cwd().resolve()
```

**Key Insight:** Config is minimal - only token and data root. No external API endpoint configuration exists yet.

---

### 4. HTTP Client / API Call Pattern

**Current State:**
- **NO external HTTP client exists** in current codebase
- **NO external API calls** are made to ThinkTrace backend
- Bot operates in **offline-first mode** with local session storage

**Import Analysis:**
```python
# main.py imports - NO requests/httpx/aiohttp
import json
import os
import logging
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telegram import Update, BotCommand  # Telegram SDK
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

import session_store  # Local persistence
```

**Key Insight:** This is a **standalone bot** with no current external integration. Future ThinkTrace integration will need to add HTTP client (likely `aiohttp` or `httpx` for async compatibility).

---

### 5. Local State / Session Handling Pattern

**File:** `session_store.py`

**Session Data Model:**
- Storage: JSON files in `sessions/` directory
- Active mapping: `sessions/active_sessions.json` maps `chat_id -> inspection_id`
- Session structure:
  ```json
  {
    "inspection_id": "AT-OTU-001-20260216-084903",
    "project_id": "PROJ-001",
    "status": "CAPTURING" | "REVIEW" | "LOCKED",
    "created_at": "ISO-8601 timestamp",
    "updated_at": "ISO-8601 timestamp",
    "header": {
      "title": "...",
      "location_text": "...",
      "weather": "Sunny",
      "datetime_override": "DD-MM-YYYY HH:MM"
    },
    "observations": [{"number": 1, "raw_text": "...", "photos": [], "include_in_report": true}],
    "actions_required": [{"number": 1, "raw_text": "...", "photos": []}],
    "actions_completed": [{"number": 1, "raw_text": "...", "photos": []}],
    "review_items": [{"number": 1, "text": "..."}]
  }
  ```

**Session Helper Functions:**
- `load_session_for_chat(chat_id)` - Load active session for a Telegram chat
- `save_session(session)` - Persist session to JSON file
- `touch_updated_at(session)` - Update timestamp
- `set_active_inspection_id(chat_id, inspection_id)` - Map chat to inspection
- `clear_active_inspection_id(chat_id)` - Remove chat-to-inspection mapping

**Key Insight:** Session storage is **file-based, JSON-serialized, per-chat scoped**. No database.

---

### 6. User Identity Mapping Logic

**Current State:**
- **NO explicit user identity mapping** exists beyond `chat_id`
- Bot operates on **Telegram chat_id as user identity**
- No OID, email, or external user reference

**Context User Data:**
- `context.user_data["mode"]` - Current interaction mode (e.g., `MODE_PROJECT_SELECT`)
- `context.user_data["mode_started_at"]` - Mode timestamp for timeout
- Various mode-specific keys (e.g., `goto_map`, `edit_map`, `info_draft`)

**Key Insight:** Future ThinkTrace integration will need to map Telegram `chat_id` to ThinkTrace `us_id` (User Space ID). This mapping may need to be stored in `projects.json` or a new `users.json` configuration file.

---

### 7. Existing Bridge to External Systems

**Current State:**
- **NO existing external system bridges**
- **NO export worker integration** with ThinkTrace (export is local to Word only)

**Export Flow:**
- `/confirm` command enqueues job in `jobs/pending/<timestamp>_<inspection_id>.json`
- Export job runs asynchronously (presumably via `worker.py`, not inspected in detail for this task)
- Export produces Word document (.docx) locally

**Key Insight:** Export is **offline/local only**. Future integration would need to export to ThinkTrace backend (e.g., `/api/ka/cases` endpoint).

---

## Implementation Recommendation for Task 4B/4C (Read-Only Setup Retrieval)

### Safest Insertion Point

**Recommended Location:** `main.py` - new command handler

**Approach:**
1. Add new command `/setup` or `/setups` to list available Inspection Setups
2. Add command handler to `build_app()`:
   ```python
   app.add_handler(CommandHandler("setup", cmd_setup_list))
   app.add_handler(CommandHandler("setups", cmd_setup_list))
   ```
3. In `cmd_setup_list()`:
   - Load project_id from current session
   - Call ThinkTrace API (new integration) to fetch setups for user
   - Display list to user
   - Add selection mode for user to pick a setup

**New Module Recommendation:**
- **DO NOT create a new helper/client module** yet
- **DO NOT add backend/API calls** in Task 4A (this discovery task)
- **Implement directly in `main.py`** for minimal initial integration
- **Consider creating a new `thinktrace_client.py` in Task 4B** when adding HTTP calls

**Why This Approach:**
- Maintains consistency with existing command handler pattern
- No refactor required
- Session context already available (chat_id → session → project_id)
- Minimal code changes for first iteration

---

## Files Inspected

1. **`main.py`** - Primary bot entry point, command handlers, mode management
2. **`session_store.py`** - Session persistence, active mapping, JSON serialization
3. **`projects.json`** - Project list (simple array of strings)
4. **`worker.py`** - Not inspected in detail (async worker, likely processes export jobs)
5. **`export_builder.py`** - Not inspected (Word document export utilities)
6. **`word_builder.py`** - Not inspected (Word document generation)
7. **`template_word_builder.py`** - Not inspected (Template processing)
8. **`rewrite_engine.py`** - Not inspected (Text processing)

---

## What Was Intentionally Not Implemented

- ❌ **NO Telegram feature changes** - Commands, handlers, modes untouched
- ❌ **NO ThinkTrace backend changes** - No new endpoints or modifications
- ❌ **NO setup retrieval implementation** - No HTTP client added, no API calls
- ❌ **NO setup selection flow** - No `/setup` command added
- ❌ **NO session injection** - Session structure unchanged
- ❌ **NO refactor** - Bot architecture preserved as-is
- ❌ **NO new helper modules** - All existing imports and structure preserved

---

## Ready for Next Task

**Task 4B/4C:** Implement read-only Inspection Setup retrieval

**Prerequisites:**
1. Add `aiohttp` or `httpx` for async HTTP client
2. Define ThinkTrace API base URL (environment variable)
3. Implement `/setup` command handler in `main.py`
4. Map `chat_id → us_id` (may require configuration update)
5. Call ThinkTrace API endpoint (to be defined in Knowledge-Accumulator-web)

**Safe Starting Point:** `main.py:build_app()` - add new `CommandHandler("setup", cmd_setup_list)`
