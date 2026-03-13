import asyncio
import json
import os
import logging
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telegram import Update, BotCommand
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import session_store
from telegram_bridge import TelegramBridgeError, fetch_inspection_setups

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("inspection-bot")

def get_data_root() -> Path:
    """Return the data root directory.

    Ultimate-version rule: all persistent data lives under INSPECTION_DATA_ROOT.
    Local dev fallback: current working directory.
    """
    root = (os.environ.get("INSPECTION_DATA_ROOT") or "").strip()
    if root:
        return Path(root).expanduser().resolve()
    return Path.cwd().resolve()


DATA_ROOT = get_data_root()

PROJECTS_PATH = DATA_ROOT / "projects.json"
TMP_PHOTOS_DIR = DATA_ROOT / "tmp_photos"

JOBS_DIR = DATA_ROOT / "jobs"
JOBS_PENDING_DIR = JOBS_DIR / "pending"
PHOTO_MAX_BYTES = 10 * 1024 * 1024  # 10MB

LOCAL_TZ = ZoneInfo("Pacific/Auckland")

# --- Input Mode Framework (Phase 1.5+) ---
MODE_NONE = "NONE"
MODE_PROJECT_SELECT = "PROJECT_SELECT"
MODE_GOTO_SELECT = "GOTO_SELECT"          # choose observation number (CAPTURING only)
MODE_ADD_REVIEW_ITEM = "ADD_REVIEW_ITEM"  # add review_items text (REVIEW only)

MODE_EDIT_CATEGORY = "EDIT_CATEGORY"      # choose category: o/ar/ac/(rev)
MODE_EDIT_SELECT = "EDIT_SELECT"          # choose item number within category
MODE_EDIT_MODE = "EDIT_MODE"              # choose 1 replace / 2 append
MODE_EDIT_TEXT = "EDIT_TEXT"              # input new text
MODE_ADD_ACTION_REQUIRED = "ADD_ACTION_REQUIRED"  # input text for new AR item
MODE_ADD_ACTION_COMPLETED = "ADD_ACTION_COMPLETED"  # input text for new AC item

# --- NEW: /info ---
MODE_INFO_INPUT = "INFO_INPUT"              # enter title (line1) + location (line2)
MODE_INFO_MENU = "INFO_MENU"                # confirm or edit
MODE_INFO_EDIT_SELECT = "INFO_EDIT_SELECT"  # choose field or accept field value (single mode)

# --- NEW: /hide ---
MODE_HIDE_SELECT = "HIDE_SELECT"            # toggle include_in_report for observations

# --- NEW: /confirm soft gate ---
MODE_CONFIRM_GATE = "CONFIRM_GATE"          # header missing prompt (1 fill now / 2 confirm anyway)
MODE_SETUP_SELECT = "SETUP_SELECT"          # choose active inspection setup from /setups

MODE_TIMEOUT_SECONDS = 120
TRUNCATE_CHARS = 60

LOCKED_MSG = "Session is LOCKED. No changes allowed."


CONFIRM_EXPORT_SUCCESS_MSG = "Confirmed and locked. Report generation in progress."
CONFIRM_EXPORT_ERROR_MSG = "Export failed. Please retry on the local machine. (Record remains locked.)"


# ----------------------------
# Basic utilities
# ----------------------------
def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(dt_str: str) -> datetime | None:
    try:
        return datetime.fromisoformat(dt_str)
    except Exception:
        return None


def _format_local_dt(dt: datetime) -> str:
    # DD-MM-YYYY HH:MM
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(LOCAL_TZ)
    return local.strftime("%d-%m-%Y %H:%M")


def _session_created_local_display(session: dict) -> str:
    created = session.get("created_at")
    if not created:
        return ""
    dt = _parse_iso(str(created))
    if not dt:
        return ""
    return _format_local_dt(dt)


def load_projects() -> list[str]:
    if not PROJECTS_PATH.exists():
        return []
    data = json.loads(PROJECTS_PATH.read_text(encoding="utf-8"))
    projects = data.get("projects", [])
    if not isinstance(projects, list):
        return []
    return [str(x) for x in projects]


def generate_inspection_id(project_id: str) -> str:
    # Example: AT-OTU-001-20260216-084903
    now = datetime.now(timezone.utc)
    return f"{project_id}-{now.strftime('%Y%m%d-%H%M%S')}"


def ensure_tmp_photo_dir(inspection_id: str) -> Path:
    path = TMP_PHOTOS_DIR / inspection_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_job_dirs() -> None:
    JOBS_PENDING_DIR.mkdir(parents=True, exist_ok=True)


def enqueue_export_job(inspection_id: str, chat_id: int, telegram_user_id: int) -> None:
    """Create a durable export job in jobs/pending.

    Job naming (locked): YYYYMMDD-HHMMSS_<inspection_id>.json
    """
    ensure_job_dirs()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"{ts}_{inspection_id}.json"
    job_path = JOBS_PENDING_DIR / filename

    job = {
        "inspection_id": inspection_id,
        "chat_id": int(chat_id),
        "telegram_user_id": int(telegram_user_id),
        "created_at": _utc_now_iso(),
        "attempt": 0,
        "next_run_at": _utc_now_iso(),
        "error_notified_first": False,
        "error_notified_final": False,
        "last_error": None,
    }

    if job_path.exists():
        raise FileExistsError(f"Job already exists: {job_path.name}")

    job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")


def _ensure_actions_defaults(session: dict) -> None:
    session.setdefault("actions_required", [])
    session.setdefault("actions_completed", [])

    # Normalize item shape (backward compatible)
    for item in session.get("actions_required", []) or []:
        item.setdefault("raw_text", "")
        item.setdefault("photos", [])
    for item in session.get("actions_completed", []) or []:
        item.setdefault("raw_text", "")
        item.setdefault("photos", [])

    # Active target defaults (backward compatible)
    if "active_kind" not in session:
        # Legacy sessions: active target was observation only
        session["active_kind"] = "OBS"
    if "active_number" not in session:
        session["active_number"] = int(session.get("active_observation", 1) or 1)


def _set_active_target(session: dict, kind: str, number: int) -> None:
    kind = (kind or "").strip().upper()
    if kind not in ("OBS", "AR", "AC"):
        raise ValueError("Invalid active kind")
    session["active_kind"] = kind
    session["active_number"] = int(number)
    if kind == "OBS":
        session["active_observation"] = int(number)


def _find_item_by_kind_number(session: dict, kind: str, number: int) -> dict | None:
    kind = (kind or "").strip().upper()
    number = int(number)
    if kind == "OBS":
        for obs in session.get("observations", []) or []:
            if int(obs.get("number", -1)) == number:
                return obs
        return None
    if kind == "AR":
        for it in session.get("actions_required", []) or []:
            if int(it.get("number", -1)) == number:
                return it
        return None
    if kind == "AC":
        for it in session.get("actions_completed", []) or []:
            if int(it.get("number", -1)) == number:
                return it
        return None
    return None


def find_active_observation(session: dict) -> dict | None:
    # Backward compatible alias: returns active OBS item if current target is OBS; otherwise None.
    _ensure_actions_defaults(session)
    if str(session.get("active_kind", "OBS")).upper() != "OBS":
        return None
    active_no = int(session.get("active_number", session.get("active_observation", 1)) or 1)
    return _find_item_by_kind_number(session, "OBS", active_no)


def _find_active_item(session: dict) -> tuple[str, dict] | tuple[None, None]:
    _ensure_actions_defaults(session)
    kind = str(session.get("active_kind", "OBS") or "OBS").upper()
    number = int(session.get("active_number", session.get("active_observation", 1)) or 1)
    item = _find_item_by_kind_number(session, kind, number)
    if item is None:
        return None, None
    return kind, item


def _truncate_one_line(text: str, limit: int = TRUNCATE_CHARS) -> str:
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    s = " ".join([line.strip() for line in s.split("\n") if line.strip()])  # flatten newlines
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 3)] + "..."




def _build_global_index(session: dict) -> tuple[dict[str, tuple[str, int]], dict[str, list[tuple[int, dict]]]]:
    """Build a global numbering map for UI selection.

    Returns:
      - map: global_no(str) -> (KIND, ref_no)
      - groups: dict with keys 'OBS','AR','AC','REV' -> list of (global_no, item_dict)
    Numbering rule (minimal, deterministic):
      Observations first, then Action Required, then Action Completed, then Review Items.
    """
    _ensure_observation_defaults(session)
    _ensure_actions_defaults(session)

    observations = sorted((session.get("observations", []) or []), key=lambda x: int(x.get("number", 0) or 0))
    actions_required = sorted((session.get("actions_required", []) or []), key=lambda x: int(x.get("number", 0) or 0))
    actions_completed = sorted((session.get("actions_completed", []) or []), key=lambda x: int(x.get("number", 0) or 0))
    review_items = sorted((session.get("review_items", []) or []), key=lambda x: int(x.get("number", 0) or 0))

    mapping: dict[str, tuple[str, int]] = {}
    groups: dict[str, list[tuple[int, dict]]] = {"OBS": [], "AR": [], "AC": [], "REV": []}

    g = 0

    for obs in observations:
        g += 1
        ref_no = int(obs.get("number", 0) or 0)
        mapping[str(g)] = ("OBS", ref_no)
        groups["OBS"].append((g, obs))

    for it in actions_required:
        g += 1
        ref_no = int(it.get("number", 0) or 0)
        mapping[str(g)] = ("AR", ref_no)
        groups["AR"].append((g, it))

    for it in actions_completed:
        g += 1
        ref_no = int(it.get("number", 0) or 0)
        mapping[str(g)] = ("AC", ref_no)
        groups["AC"].append((g, it))

    for it in review_items:
        g += 1
        ref_no = int(it.get("number", 0) or 0)
        mapping[str(g)] = ("REV", ref_no)
        groups["REV"].append((g, it))

    return mapping, groups


def _render_grouped_global_list(session: dict, *, include_reply_hint: bool = True) -> tuple[str, dict[str, tuple[str, int]]]:
    """Render segmented list with global numbering, and return (text, map)."""
    mapping, groups = _build_global_index(session)

    lines: list[str] = []

    def _render_section(title: str, key: str, text_key: str, photos_key: str | None = None) -> None:
        items = groups.get(key) or []
        if not items:
            return
        lines.append(f"[{title}]")
        for gno, it in items:
            raw = (it.get(text_key, "") or "").strip()
            preview = _truncate_one_line(raw)
            phs: list[str] = []
            if photos_key:
                phs = it.get(photos_key, []) or []
            ph_text = f" ({', '.join(phs)})" if phs else ""
            lines.append(f"{gno}. {preview}{ph_text}")
        lines.append("")

    _render_section("Observations", "OBS", "raw_text", "photos")
    _render_section("Action Required", "AR", "raw_text", "photos")
    _render_section("Action Completed", "AC", "raw_text", "photos")
    _render_section("Review Items", "REV", "text", None)

    if include_reply_hint and mapping:
        lines.append("Reply a number:")

    text = "\n".join(lines).strip() if lines else "No items."
    return text, mapping

def _ensure_observation_defaults(session: dict) -> None:
    # Backward compatible: older sessions may not have include_in_report
    for obs in session.get("observations", []) or []:
        if "include_in_report" not in obs:
            obs["include_in_report"] = True
    _ensure_actions_defaults(session)


def _get_header(session: dict) -> dict:
    header = session.setdefault("header", {})
    # Define defaults (keep as light as possible)
    header.setdefault("title", "")
    header.setdefault("location_text", "")
    header.setdefault("location_general", "")
    header.setdefault("weather", None)  # category string or None
    header.setdefault("weather_is_manual", False)
    header.setdefault("datetime_override", None)  # "DD-MM-YYYY HH:MM"
    header.setdefault("info_confirmed_at", None)  # ISO UTC
    return header


def _header_missing_title_or_location(session: dict) -> bool:
    header = _get_header(session)
    return (not str(header.get("title", "")).strip()) or (not str(header.get("location_text", "")).strip())


def _parse_two_lines(text: str) -> tuple[str, str] | None:
    parts = [line.strip() for line in text.splitlines() if line.strip()]
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


def _extract_general_location(location_text: str) -> str | None:
    # Low accuracy on purpose: use last comma-separated part, or last token.
    s = (location_text or "").strip()
    if not s:
        return None
    if "," in s:
        cand = s.split(",")[-1].strip()
        return cand if cand else None
    tokens = s.split()
    return tokens[-1].strip() if tokens else None


def get_weather_category(location_general: str, basis_iso_utc: str | None) -> str | None:
    """
    Stub for MVP.
    Option 3 rule: if we can't determine general location, return None.
    Later you can replace this with a real weather lookup.
    """
    _ = basis_iso_utc
    if not (location_general or "").strip():
        return None
    return None


# ----------------------------
# Mode helpers
# ----------------------------
def _now_utc_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def set_mode(context: ContextTypes.DEFAULT_TYPE, mode: str) -> None:
    context.user_data["mode"] = mode
    context.user_data["mode_started_at"] = _now_utc_ts()


def clear_mode(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["mode"] = MODE_NONE
    context.user_data.pop("mode_started_at", None)


def get_mode(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get("mode", MODE_NONE)


def is_mode_timed_out(context: ContextTypes.DEFAULT_TYPE) -> bool:
    mode = get_mode(context)
    if mode == MODE_NONE:
        return False
    started = context.user_data.get("mode_started_at", None)
    if started is None:
        return False
    return (_now_utc_ts() - float(started)) > MODE_TIMEOUT_SECONDS


def _clear_all_pending(context: ContextTypes.DEFAULT_TYPE) -> None:
    # Edit metadata
    context.user_data.pop("edit_selected_kind", None)
    context.user_data.pop("edit_selected_index", None)
    context.user_data.pop("edit_mode_choice", None)
    context.user_data.pop("edit_combined", None)

    # /info draft + edit field
    context.user_data.pop("info_draft", None)
    context.user_data.pop("info_edit_field", None)

    # /confirm gate
    context.user_data.pop("confirm_pending", None)
    context.user_data.pop("setup_selection_options", None)


# ----------------------------
# Status helpers
# ----------------------------
def status_is_locked(session: dict | None) -> bool:
    return bool(session) and session.get("status") == "LOCKED"


def status_is_review(session: dict | None) -> bool:
    return bool(session) and session.get("status") == "REVIEW"


def status_is_capturing(session: dict | None) -> bool:
    return bool(session) and session.get("status") == "CAPTURING"


# ----------------------------
# Telegram command menu (/menu)
# ----------------------------
async def post_init(application) -> None:
    # IMPORTANT: keep this exact order (as per your locked workflow + updates)
    commands = [
        BotCommand("start", "Start"),
        BotCommand("setups", "List active inspection setups"),
        BotCommand("currentsetup", "Show selected inspection setup"),
        BotCommand("clearsetup", "Clear selected inspection setup"),
        BotCommand("new", "Add new inspection item"),
        BotCommand("action_required", "add action required item"),
        BotCommand("action_completed", "add action completed item"),
        BotCommand("goto", "Select item to edit"),
        BotCommand("done", "Finish inspection"),
        BotCommand("review", "Show summary"),
        BotCommand("info", "Edit inspection Info"),
        BotCommand("edit", "Edit selected item"),
        BotCommand("add", "Add new comment (no photos)"),
        BotCommand("hide", "Hide items in report"),
        BotCommand("confirm", "Submit (non-editable)"),
        BotCommand("cancel", "Cancel current input"),
    ]
    await application.bot.set_my_commands(commands)

# ----------------------------
# Commands

# ----------------------------
# Commands
# ----------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    selected_setup = context.user_data.get("selected_setup")
    if selected_setup:
        chat_id = update.effective_chat.id
        project_id = str(selected_setup.get("project_id"))
        session, session_selected_setup = _build_new_session(project_id, selected_setup)
        inspection_id = str(session["inspection_id"])

        session_store.set_active_inspection_id(chat_id, inspection_id)
        session_store.save_session(session)

        context.user_data.pop("projects_cache", None)
        clear_mode(context)
        _clear_all_pending(context)

        await update.message.reply_text(
            "Session started from selected setup.\n"
            f"Project: {project_id}\n"
            f"Inspection: {inspection_id}\n"
            "CAPTURING.\n"
            f"Applied setup: {session_selected_setup['setup_name']} ({session_selected_setup['setup_id']})."
        )
        return

    projects = load_projects()
    if not projects:
        await update.message.reply_text("No projects found in projects.json.")
        return

    lines = ["Select a project by number:"]
    for i, p in enumerate(projects, start=1):
        lines.append(f"{i}. {p}")

    context.user_data["projects_cache"] = projects
    set_mode(context, MODE_PROJECT_SELECT)
    await update.message.reply_text("\n".join(lines))


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if get_mode(context) == MODE_NONE:
        await update.message.reply_text("Nothing to cancel.")
        return
    clear_mode(context)
    _clear_all_pending(context)
    await update.message.reply_text("Cancelled.")


def _render_setups_message(setups: list) -> str:
    if not setups:
        return "No active inspection setups found."

    lines: list[str] = ["Active inspection setups:"]
    for idx, setup in enumerate(setups, start=1):
        template_id = (
            str(setup.selected_template_id)
            if setup.selected_template_id is not None
            else "None"
        )
        lines.append("")
        lines.append(f"{idx}. {setup.setup_name}")
        lines.append(f"project_id: {setup.project_id}")
        lines.append(f"selected_template_id: {template_id}")
    lines.append("")
    lines.append("Reply with a number to select a setup.")
    return "\n".join(lines)


def _serialize_setup_summary(setup) -> dict[str, str | bool | None]:
    return {
        "setup_id": str(setup.setup_id),
        "setup_name": str(setup.setup_name),
        "project_id": str(setup.project_id),
        "selected_template_id": (
            None if setup.selected_template_id is None else str(setup.selected_template_id)
        ),
        "is_active": bool(setup.is_active),
    }


def _render_selected_setup_message(selected_setup: dict | None) -> str:
    if not selected_setup:
        return "No inspection setup selected."

    template_id = selected_setup.get("selected_template_id")
    template_text = "None" if template_id is None else str(template_id)
    return (
        "Current inspection setup:\n\n"
        f"setup_name: {selected_setup.get('setup_name')}\n"
        f"project_id: {selected_setup.get('project_id')}\n"
        f"selected_template_id: {template_text}"
    )


def _build_session_selected_setup(selected_setup: dict | None) -> dict[str, str | None] | None:
    if not selected_setup:
        return None

    return {
        "setup_id": str(selected_setup.get("setup_id")),
        "setup_name": str(selected_setup.get("setup_name")),
        "project_id": str(selected_setup.get("project_id")),
        "selected_template_id": (
            None
            if selected_setup.get("selected_template_id") is None
            else str(selected_setup.get("selected_template_id"))
        ),
    }


def _build_new_session(
    project_id: str, selected_setup: dict | None = None
) -> tuple[dict, dict[str, str | None] | None]:
    inspection_id = generate_inspection_id(project_id)
    session_selected_setup = _build_session_selected_setup(selected_setup)
    now_iso = _utc_now_iso()

    session = {
        "inspection_id": inspection_id,
        "project_id": project_id,
        "status": "CAPTURING",
        "created_at": now_iso,
        "updated_at": now_iso,
        "photo_counter": 1,
        "active_observation": 1,
        "active_kind": "OBS",
        "active_number": 1,
        "actions_required": [],
        "actions_completed": [],
        "observations": [{"number": 1, "raw_text": "", "photos": [], "include_in_report": True}],
        "review_items": [],
        "header": {
            "title": "",
            "location_text": "",
            "location_general": "",
            "weather": None,
            "weather_is_manual": False,
            "datetime_override": None,
            "info_confirmed_at": None,
        },
    }
    if session_selected_setup is not None:
        session["selected_setup"] = session_selected_setup

    return session, session_selected_setup


def _is_unmapped_bridge_error(exc: TelegramBridgeError) -> bool:
    message = str(exc).strip().lower()
    return "unmapped" in message or "not mapped" in message


async def cmd_setups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_user = update.effective_user
    message = update.message

    if telegram_user is None or message is None:
        return

    try:
        setups = await asyncio.to_thread(
            fetch_inspection_setups,
            int(telegram_user.id),
            active_only=True,
        )
    except TelegramBridgeError as exc:
        if _is_unmapped_bridge_error(exc):
            await message.reply_text(
                "Your Telegram account is not mapped to a ThinkTrace user."
            )
            return

        logger.exception(
            "Failed to load inspection setups for Telegram user %s: %s",
            telegram_user.id,
            exc,
        )
        await message.reply_text(
            "Unable to load inspection setups right now. Please try again later."
        )
        return

    if not setups:
        clear_mode(context)
        context.user_data.pop("setup_selection_options", None)
        await message.reply_text(_render_setups_message(setups))
        return

    context.user_data["setup_selection_options"] = [
        _serialize_setup_summary(setup) for setup in setups
    ]
    set_mode(context, MODE_SETUP_SELECT)
    await message.reply_text(_render_setups_message(setups))


async def cmd_currentsetup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return

    await message.reply_text(
        _render_selected_setup_message(context.user_data.get("selected_setup"))
    )


async def cmd_clearsetup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return

    context.user_data.pop("selected_setup", None)
    context.user_data.pop("setup_selection_options", None)
    if get_mode(context) == MODE_SETUP_SELECT:
        clear_mode(context)
    await message.reply_text("Cleared selected inspection setup.")


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    session = session_store.load_session_for_chat(chat_id)

    if not session:
        await update.message.reply_text("No active session. Use /start.")
        return
    if status_is_locked(session):
        await update.message.reply_text(LOCKED_MSG)
        return
    if not status_is_capturing(session):
        await update.message.reply_text("New inspection items only in CAPTURING.")
        return

    observations = session.setdefault("observations", [])
    next_no = len(observations) + 1
    observations.append({"number": next_no, "raw_text": "", "photos": [], "include_in_report": True})
    session["active_observation"] = next_no
    session["active_kind"] = "OBS"
    session["active_number"] = next_no

    session_store.touch_updated_at(session)
    session_store.save_session_for_chat(chat_id, session)

    await update.message.reply_text(f"Inspection item {next_no} started.")



async def cmd_action_required(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add a new Action Required item. Allowed in CAPTURING and REVIEW."""
    chat_id = update.effective_chat.id
    session = session_store.load_session_for_chat(chat_id)

    if not session:
        await update.message.reply_text("No active session. Use /start.")
        return
    if status_is_locked(session):
        await update.message.reply_text(LOCKED_MSG)
        return

    _ensure_observation_defaults(session)
    _ensure_actions_defaults(session)

    items = session.get("actions_required", []) or []
    next_no = len(items) + 1
    items.append({"number": next_no, "raw_text": "", "photos": []})
    session["actions_required"] = items
    _set_active_target(session, "AR", next_no)

    session_store.touch_updated_at(session)
    session_store.save_session_for_chat(chat_id, session)

    if status_is_capturing(session):
        clear_mode(context)
        await update.message.reply_text(f"Action Required {next_no} started.")
        return

    # REVIEW: prompt for initial text
    set_mode(context, MODE_ADD_ACTION_REQUIRED)
    context.user_data["new_action_number"] = next_no
    await update.message.reply_text("Send text for the new Action Required item.")


async def cmd_action_completed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add a new Action Completed item. Allowed in CAPTURING and REVIEW."""
    chat_id = update.effective_chat.id
    session = session_store.load_session_for_chat(chat_id)

    if not session:
        await update.message.reply_text("No active session. Use /start.")
        return
    if status_is_locked(session):
        await update.message.reply_text(LOCKED_MSG)
        return

    _ensure_observation_defaults(session)
    _ensure_actions_defaults(session)

    items = session.get("actions_completed", []) or []
    next_no = len(items) + 1
    items.append({"number": next_no, "raw_text": "", "photos": []})
    session["actions_completed"] = items
    _set_active_target(session, "AC", next_no)

    session_store.touch_updated_at(session)
    session_store.save_session_for_chat(chat_id, session)

    if status_is_capturing(session):
        clear_mode(context)
        await update.message.reply_text(f"Action Completed {next_no} started.")
        return

    # REVIEW: prompt for initial text
    set_mode(context, MODE_ADD_ACTION_COMPLETED)
    context.user_data["new_action_number"] = next_no
    await update.message.reply_text("Send text for the new Action Completed item.")


async def cmd_go(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """CAPTURING only: select active item for auto-append and photos.

    UI rule (locked): same as /goto and /edit — segmented display + global single numbering.
    User replies with a number only.
    """
    chat_id = update.effective_chat.id
    session = session_store.load_session_for_chat(chat_id)

    if not session:
        await update.message.reply_text("No active session. Use /start.")
        return
    if status_is_locked(session):
        await update.message.reply_text(LOCKED_MSG)
        return
    if not status_is_capturing(session):
        await update.message.reply_text("In REVIEW, use /edit to select an item.")
        return

    # Same UI as /goto: show segmented list + global numbering.
    _ensure_observation_defaults(session)
    _ensure_actions_defaults(session)

    text, mapping = _render_grouped_global_list(session, include_reply_hint=True)
    if not mapping:
        await update.message.reply_text("No inspection items. Use /new.")
        return

    # Reuse MODE_GOTO_SELECT mapping pipeline.
    context.user_data["goto_map"] = mapping
    set_mode(context, MODE_GOTO_SELECT)
    await update.message.reply_text(text)


async def cmd_goto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    CAPTURING only: select active item for auto-append (observations + actions).
    UI: segmented display + global single numbering. Reply with a number.
    """
    chat_id = update.effective_chat.id
    session = session_store.load_session_for_chat(chat_id)

    if not session:
        await update.message.reply_text("No active session. Use /start.")
        return
    if status_is_locked(session):
        await update.message.reply_text(LOCKED_MSG)
        return
    if not status_is_capturing(session):
        await update.message.reply_text("In REVIEW, use /edit to select an item.")
        return

    _ensure_observation_defaults(session)
    _ensure_actions_defaults(session)

    text, mapping = _render_grouped_global_list(session, include_reply_hint=True)
    if not mapping:
        await update.message.reply_text("No inspection items. Use /new.")
        return

    context.user_data["goto_map"] = mapping
    set_mode(context, MODE_GOTO_SELECT)
    await update.message.reply_text(text)


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    session = session_store.load_session_for_chat(chat_id)

    if not session:
        await update.message.reply_text("No active session. Use /start.")
        return
    if status_is_locked(session):
        await update.message.reply_text(LOCKED_MSG)
        return
    if not status_is_capturing(session):
        await update.message.reply_text("Already in REVIEW.")
        return

    session["status"] = "REVIEW"
    session_store.touch_updated_at(session)
    session_store.save_session_for_chat(chat_id, session)

    await update.message.reply_text("Finished. Session is now in REVIEW.")


def _render_review_message(session: dict) -> str:
    _ensure_observation_defaults(session)
    _ensure_actions_defaults(session)
    header = _get_header(session)

    lines: list[str] = []

    # Reminder (title OR location missing)
    if _header_missing_title_or_location(session):
        lines.append("Reminder: header info not set. Use /info.")
        lines.append("")

    # Short header
    title = str(header.get("title", "") or "").strip()
    location_text = str(header.get("location_text", "") or "").strip()

    if title:
        lines.append(f"Title: {title}")
    if location_text:
        lines.append(f"Location: {location_text}")

    if isinstance(header.get("weather", None), str) and str(header.get("weather")).strip():
        lines.append(f"Weather: {str(header.get('weather')).strip()}")

    if lines:
        lines.append("")

    # Segmented list (global numbering) including review items
    list_text, _ = _render_grouped_global_list(session, include_reply_hint=False)
    lines.append(list_text)

    return "\n".join(lines).strip() if lines else "No items."



async def cmd_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    session = session_store.load_session_for_chat(chat_id)

    if not session:
        await update.message.reply_text("No active session. Use /start.")
        return

    message = _render_review_message(session)
    await update.message.reply_text(message)


async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    REVIEW only: capture header info (title + location) in 2 lines.
    Then show summary, allow confirm or edit.
    """
    chat_id = update.effective_chat.id
    session = session_store.load_session_for_chat(chat_id)

    if not session:
        await update.message.reply_text("No active session. Use /start.")
        return
    if status_is_locked(session):
        await update.message.reply_text(LOCKED_MSG)
        return
    if not status_is_review(session):
        await update.message.reply_text("Use /done to enter REVIEW first.")
        return

    # Reset /info flow state
    context.user_data.pop("info_draft", None)
    context.user_data.pop("info_edit_field", None)

    set_mode(context, MODE_INFO_INPUT)
    await update.message.reply_text(
        "Enter inspection title (line 1)\n"
        "Enter location (line 2, detailed address allowed)\n\n"
        "Send both lines in one message."
    )


def _build_info_draft_from_input(session: dict, title: str, location_text: str) -> dict:
    created_iso = session.get("created_at")
    created_display = _session_created_local_display(session)

    location_general = _extract_general_location(location_text) or ""
    weather = None
    if location_general:
        weather = get_weather_category(location_general, str(created_iso) if created_iso else None)

    draft = {
        "title": title.strip(),
        "location_text": location_text.strip(),
        "location_general": location_general,
        "weather": weather,  # may be None
        "weather_is_manual": False,
        "datetime_display": created_display,
        "datetime_override": None,
    }
    return draft


def _render_info_summary(session: dict, draft: dict) -> str:
    dt_display = draft.get("datetime_display") or _session_created_local_display(session) or ""
    if draft.get("datetime_override"):
        dt_display = str(draft["datetime_override"])

    weather_display = draft.get("weather")
    if isinstance(weather_display, str) and weather_display.strip():
        w_text = weather_display.strip()
    else:
        w_text = "Unable to determine"

    return (
        "Inspection title:\n"
        f"{draft.get('title','')}\n\n"
        "Date & time:\n"
        f"{dt_display}\n\n"
        "Location:\n"
        f"{draft.get('location_text','')}\n\n"
        "Weather:\n"
        f"{w_text}\n\n"
        "1) Confirm\n"
        "2) Edit"
    )


async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Select an item to edit.

    UI: segmented display + global single numbering. Reply with a number.
    Allowed in CAPTURING and REVIEW. Not allowed in LOCKED.
    """
    chat_id = update.effective_chat.id
    session = session_store.load_session_for_chat(chat_id)

    if not session:
        await update.message.reply_text("No active session. Use /start.")
        return
    if status_is_locked(session):
        await update.message.reply_text(LOCKED_MSG)
        return

    _ensure_observation_defaults(session)
    _ensure_actions_defaults(session)

    # Clear previous edit state
    context.user_data.pop("edit_map", None)
    context.user_data.pop("edit_selected_kind", None)
    context.user_data.pop("edit_selected_number", None)
    context.user_data.pop("edit_selected_index", None)
    context.user_data.pop("edit_mode_choice", None)

    text, mapping = _render_grouped_global_list(session, include_reply_hint=True)
    if not mapping:
        await update.message.reply_text("No items to edit.")
        return

    context.user_data["edit_map"] = mapping
    set_mode(context, MODE_EDIT_SELECT)
    await update.message.reply_text(text)


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Add new review-only item (review_items). REVIEW only.
    This is the original /item behavior.
    """
    chat_id = update.effective_chat.id
    session = session_store.load_session_for_chat(chat_id)

    if not session:
        await update.message.reply_text("No active session. Use /start.")
        return
    if status_is_locked(session):
        await update.message.reply_text(LOCKED_MSG)
        return
    if not status_is_review(session):
        await update.message.reply_text("Use /done to enter REVIEW first.")
        return

    set_mode(context, MODE_ADD_REVIEW_ITEM)
    await update.message.reply_text("Send text for the new item.")


def _render_hide_list(session: dict) -> tuple[str, dict[str, tuple[str, int]]]:
    """Render /hide UI.

    UI rule: segmented display + global single numbering.
    Behaviour: only Observations support toggling include_in_report.
    """
    _ensure_observation_defaults(session)
    _ensure_actions_defaults(session)

    mapping, groups = _build_global_index(session)

    lines: list[str] = []
    lines.append("Hide items in report")
    lines.append("")

    def _render_obs():
        items = groups.get("OBS") or []
        if not items:
            return
        lines.append("[Observations]")
        for gno, obs in items:
            state = "[SHOW]" if bool(obs.get("include_in_report", True)) else "[HIDE]"
            preview = _truncate_one_line(str(obs.get("raw_text", "") or ""))
            phs = obs.get("photos", []) or []
            ph_text = f" ({', '.join(phs)})" if phs else ""
            lines.append(f"{gno}. {state} {preview}{ph_text}")
        lines.append("")

    def _render_actions(title: str, key: str):
        items = groups.get(key) or []
        if not items:
            return
        lines.append(f"[{title}]")
        for gno, it in items:
            preview = _truncate_one_line(str(it.get("raw_text", "") or ""))
            phs = it.get("photos", []) or []
            ph_text = f" ({', '.join(phs)})" if phs else ""
            lines.append(f"{gno}. {preview}{ph_text}")
        lines.append("")

    def _render_review():
        items = groups.get("REV") or []
        if not items:
            return
        lines.append("[Review Items]")
        for gno, it in items:
            preview = _truncate_one_line(str(it.get("text", "") or ""))
            lines.append(f"{gno}. {preview}")
        lines.append("")

    if not mapping:
        return "No items.", {}

    _render_obs()
    _render_actions("Action Required", "AR")
    _render_actions("Action Completed", "AC")
    _render_review()

    lines.append("Reply a number to toggle (observations only). Reply 0 to finish.")
    return "\n".join(lines).strip(), mapping


async def cmd_hide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    session = session_store.load_session_for_chat(chat_id)

    if not session:
        await update.message.reply_text("No active session. Use /start.")
        return
    if status_is_locked(session):
        await update.message.reply_text(LOCKED_MSG)
        return
    if not status_is_review(session):
        await update.message.reply_text("Use /done to enter REVIEW first.")
        return

    set_mode(context, MODE_HIDE_SELECT)
    text, mapping = _render_hide_list(session)
    context.user_data["hide_map"] = mapping
    await update.message.reply_text(text)



async def cmd_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    session = session_store.load_session_for_chat(chat_id)

    if not session:
        await update.message.reply_text("No active session. Use /start.")
        return
    if status_is_locked(session):
        await update.message.reply_text("Session is already LOCKED.")
        return
    if not status_is_review(session):
        await update.message.reply_text("Use /done to enter REVIEW first.")
        return

    # Soft gate (2B): if title or location missing, prompt.
    if _header_missing_title_or_location(session):
        context.user_data["confirm_pending"] = True
        set_mode(context, MODE_CONFIRM_GATE)
        await update.message.reply_text(
            "Header info missing (title/location).\n"
            "1) Fill now (/info)\n"
            "2) Confirm anyway"
        )
        return

    # /confirm == /export
    session["status"] = "LOCKED"
    session["confirmed_at"] = _utc_now_iso()
    session_store.touch_updated_at(session)
    session_store.save_session_for_chat(chat_id, session)

    await update.message.reply_text(CONFIRM_EXPORT_SUCCESS_MSG)
    try:
        enqueue_export_job(
            str(session.get("inspection_id")),
            chat_id,
            int(update.effective_user.id),
        )
    except Exception as e:
        logger.exception("Failed to enqueue export job: %s", e)
        await update.message.reply_text(CONFIRM_EXPORT_ERROR_MSG)


# ----------------------------
# Text handler (mode-first)
# ----------------------------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or update.message.text is None:
        return

    text = update.message.text.strip()
    if not text:
        return

    # Global mode timeout
    if is_mode_timed_out(context):
        clear_mode(context)
        _clear_all_pending(context)
        await update.message.reply_text("Timed out. Please try again.")
        return

    chat_id = update.effective_chat.id
    mode = get_mode(context)

    # MODE has priority (prevents CAPTURING auto-append duplicates)
    if mode != MODE_NONE:
        # PROJECT_SELECT
        if mode == MODE_PROJECT_SELECT:
            projects = context.user_data.get("projects_cache") or load_projects()
            try:
                choice = int(text)
            except ValueError:
                await update.message.reply_text("Please reply with a number.")
                return

            if choice < 1 or choice > len(projects):
                await update.message.reply_text("Invalid number. Please try again.")
                return

            project_id = projects[choice - 1]
            session, session_selected_setup = _build_new_session(
                project_id, context.user_data.get("selected_setup")
            )
            inspection_id = str(session["inspection_id"])

            session_store.set_active_inspection_id(chat_id, inspection_id)
            session_store.save_session(session)

            context.user_data.pop("projects_cache", None)
            clear_mode(context)
            _clear_all_pending(context)

            lines = [
                "Session started.",
                f"Project: {project_id}",
                f"Inspection: {inspection_id}",
                "CAPTURING.",
            ]
            if session_selected_setup is not None:
                lines.append(
                    f"Applied setup: {session_selected_setup['setup_name']} ({session_selected_setup['setup_id']})."
                )

            await update.message.reply_text("\n".join(lines))
            return

        if mode == MODE_SETUP_SELECT:
            options = context.user_data.get("setup_selection_options") or []
            if not options:
                clear_mode(context)
                await update.message.reply_text("No setup list is active. Use /setups.")
                return

            try:
                choice = int(text)
            except ValueError:
                await update.message.reply_text("Please reply with a setup number.")
                return

            if choice < 1 or choice > len(options):
                await update.message.reply_text("Invalid setup number. Please try again.")
                return

            selected_setup = dict(options[choice - 1])
            context.user_data["selected_setup"] = selected_setup
            context.user_data.pop("setup_selection_options", None)
            clear_mode(context)
            await update.message.reply_text(
                f"Selected setup: {selected_setup['setup_name']} ({selected_setup['project_id']})."
            )
            return

        # Other modes require an active session
        session = session_store.load_session_for_chat(chat_id)
        if not session:
            await update.message.reply_text("No active session. Use /start.")
            clear_mode(context)
            _clear_all_pending(context)
            return
        if status_is_locked(session):
            await update.message.reply_text(LOCKED_MSG)
            clear_mode(context)
            _clear_all_pending(context)
            return

        # Ensure backward compatibility fields
        _ensure_observation_defaults(session)
        _get_header(session)

        # CONFIRM_GATE
        if mode == MODE_CONFIRM_GATE:
            if text not in ("1", "2"):
                await update.message.reply_text("Reply 1 or 2.")
                return

            if text == "1":
                clear_mode(context)
                context.user_data.pop("confirm_pending", None)
                _clear_all_pending(context)
                await cmd_info(update, context)
                return

            clear_mode(context)
            context.user_data.pop("confirm_pending", None)

            # /confirm == /export
            session["status"] = "LOCKED"
            session["confirmed_at"] = _utc_now_iso()
            session_store.touch_updated_at(session)
            session_store.save_session_for_chat(chat_id, session)

            await update.message.reply_text(CONFIRM_EXPORT_SUCCESS_MSG)
            try:
                enqueue_export_job(
                    str(session.get("inspection_id")),
                    chat_id,
                    int(update.effective_user.id),
                )
            except Exception as e:
                logger.exception("Failed to enqueue export job: %s", e)
                await update.message.reply_text(CONFIRM_EXPORT_ERROR_MSG)
            return

        # GOTO_SELECT (CAPTURING only) - segmented display + global numbering
        if mode == MODE_GOTO_SELECT:
            if not status_is_capturing(session):
                clear_mode(context)
                await update.message.reply_text("In REVIEW, use /edit to select an item.")
                return

            goto_map = context.user_data.get("goto_map") or {}
            if not goto_map:
                goto_map, _groups = _build_global_index(session)
                context.user_data["goto_map"] = goto_map

            try:
                pick = int(text)
            except ValueError:
                await update.message.reply_text("Please reply with a number.")
                return

            if str(pick) not in goto_map:
                await update.message.reply_text("Invalid number. Please try again.")
                return

            kind, ref_no = goto_map[str(pick)]
            if kind not in ("OBS", "AR", "AC"):
                await update.message.reply_text("Invalid selection for /goto.")
                return

            _set_active_target(session, kind, int(ref_no))
            session_store.touch_updated_at(session)
            session_store.save_session_for_chat(chat_id, session)

            clear_mode(context)
            await update.message.reply_text("Selected.")
            return

        # ADD_REVIEW_ITEM (REVIEW only)
        if mode == MODE_ADD_REVIEW_ITEM:
            if not status_is_review(session):
                clear_mode(context)
                await update.message.reply_text("Use /done to enter REVIEW first.")
                return

            items = session.setdefault("review_items", [])
            next_no = len(items) + 1
            items.append({"number": next_no, "text": text})

            session_store.touch_updated_at(session)
            session_store.save_session_for_chat(chat_id, session)

            clear_mode(context)
            await update.message.reply_text("Added.")
            return

        # HIDE_SELECT (REVIEW only)
        if mode == MODE_HIDE_SELECT:
            if not status_is_review(session):
                clear_mode(context)
                await update.message.reply_text("Use /done to enter REVIEW first.")
                return

            try:
                pick = int(text)
            except ValueError:
                await update.message.reply_text("Please reply with a number.")
                return

            if pick == 0:
                clear_mode(context)
                context.user_data.pop("hide_map", None)
                await update.message.reply_text("Done.")
                return

            hide_map = context.user_data.get("hide_map") or {}
            if not hide_map:
                # Rebuild if missing
                _ensure_observation_defaults(session)
                _ensure_actions_defaults(session)
                hide_map, _groups = _build_global_index(session)
                context.user_data["hide_map"] = hide_map

            if str(pick) not in hide_map:
                await update.message.reply_text("Invalid number. Please try again.")
                return

            kind, ref_no = hide_map[str(pick)]
            if kind != "OBS":
                text2, mapping2 = _render_hide_list(session)
                context.user_data["hide_map"] = mapping2
                await update.message.reply_text("Only observations can be hidden.\n\n" + text2)
                return

            obs = _find_item_by_kind_number(session, "OBS", int(ref_no))
            if obs is None:
                await update.message.reply_text("Invalid selection. Please try again.")
                return

            cur = bool(obs.get("include_in_report", True))
            obs["include_in_report"] = (not cur)

            session_store.touch_updated_at(session)
            session_store.save_session_for_chat(chat_id, session)

            text2, mapping2 = _render_hide_list(session)
            context.user_data["hide_map"] = mapping2
            await update.message.reply_text(text2)
            return

        # INFO_INPUT
        if mode == MODE_INFO_INPUT:
            if not status_is_review(session):
                clear_mode(context)
                await update.message.reply_text("Use /done to enter REVIEW first.")
                return

            parsed = _parse_two_lines(text)
            if not parsed:
                await update.message.reply_text("Please send 2 lines: title (line 1) and location (line 2).")
                return

            title, location_text = parsed
            draft = _build_info_draft_from_input(session, title, location_text)
            context.user_data["info_draft"] = draft
            context.user_data.pop("info_edit_field", None)

            set_mode(context, MODE_INFO_MENU)
            await update.message.reply_text(_render_info_summary(session, draft))
            return

        # INFO_MENU
        if mode == MODE_INFO_MENU:
            draft = context.user_data.get("info_draft")
            if not isinstance(draft, dict):
                clear_mode(context)
                await update.message.reply_text("Cancelled.")
                return

            if text not in ("1", "2"):
                await update.message.reply_text("Reply 1 or 2.")
                return

            if text == "1":
                header = _get_header(session)
                header["title"] = str(draft.get("title", "") or "")
                header["location_text"] = str(draft.get("location_text", "") or "")
                header["location_general"] = str(draft.get("location_general", "") or "")
                header["weather"] = draft.get("weather", None)
                header["weather_is_manual"] = bool(draft.get("weather_is_manual", False))
                header["datetime_override"] = draft.get("datetime_override", None)
                header["info_confirmed_at"] = _utc_now_iso()

                session_store.touch_updated_at(session)
                session_store.save_session_for_chat(chat_id, session)

                clear_mode(context)
                _clear_all_pending(context)
                await update.message.reply_text("Info saved.")
                return

            set_mode(context, MODE_INFO_EDIT_SELECT)
            context.user_data["info_edit_field"] = None
            await update.message.reply_text(
                "1) Edit inspection title\n"
                "2) Edit date & time\n"
                "3) Edit location\n"
                "4) Edit weather\n"
                "5) Back"
            )
            return

        # INFO_EDIT_SELECT
        if mode == MODE_INFO_EDIT_SELECT:
            draft = context.user_data.get("info_draft")
            if not isinstance(draft, dict):
                clear_mode(context)
                await update.message.reply_text("Cancelled.")
                return

            edit_field = context.user_data.get("info_edit_field", None)

            if edit_field in ("TITLE", "DATETIME", "LOCATION", "WEATHER"):
                if edit_field == "TITLE":
                    draft["title"] = text.strip()
                    context.user_data["info_edit_field"] = None
                    set_mode(context, MODE_INFO_MENU)
                    await update.message.reply_text(_render_info_summary(session, draft))
                    return

                if edit_field == "DATETIME":
                    try:
                        datetime.strptime(text.strip(), "%d-%m-%Y %H:%M")
                    except ValueError:
                        await update.message.reply_text("Invalid format. Use DD-MM-YYYY HH:MM.")
                        return
                    draft["datetime_override"] = text.strip()
                    context.user_data["info_edit_field"] = None
                    set_mode(context, MODE_INFO_MENU)
                    await update.message.reply_text(_render_info_summary(session, draft))
                    return

                if edit_field == "LOCATION":
                    loc = text.strip()
                    draft["location_text"] = loc
                    loc_gen = _extract_general_location(loc) or ""
                    draft["location_general"] = loc_gen

                    draft["weather_is_manual"] = False
                    weather = None
                    if loc_gen:
                        created_iso = session.get("created_at")
                        weather = get_weather_category(loc_gen, str(created_iso) if created_iso else None)
                    draft["weather"] = weather

                    context.user_data["info_edit_field"] = None
                    set_mode(context, MODE_INFO_MENU)
                    await update.message.reply_text(_render_info_summary(session, draft))
                    return

                if edit_field == "WEATHER":
                    if text not in ("1", "2", "3", "4", "5"):
                        await update.message.reply_text("Reply 1-5.")
                        return
                    mapping = {
                        "1": "Sunny",
                        "2": "Cloudy",
                        "3": "Light Rain",
                        "4": "Heavy Rain",
                        "5": None,
                    }
                    draft["weather"] = mapping[text]
                    draft["weather_is_manual"] = True
                    context.user_data["info_edit_field"] = None
                    set_mode(context, MODE_INFO_MENU)
                    await update.message.reply_text(_render_info_summary(session, draft))
                    return

            if text not in ("1", "2", "3", "4", "5"):
                await update.message.reply_text("Reply 1-5.")
                return

            if text == "5":
                set_mode(context, MODE_INFO_MENU)
                await update.message.reply_text(_render_info_summary(session, draft))
                return

            if text == "1":
                context.user_data["info_edit_field"] = "TITLE"
                await update.message.reply_text("Send new inspection title.")
                return

            if text == "2":
                context.user_data["info_edit_field"] = "DATETIME"
                await update.message.reply_text("Send new date & time (DD-MM-YYYY HH:MM).")
                return

            if text == "3":
                context.user_data["info_edit_field"] = "LOCATION"
                await update.message.reply_text("Send new location (detailed address allowed).")
                return

            context.user_data["info_edit_field"] = "WEATHER"
            await update.message.reply_text(
                "Choose weather:\n"
                "1) Sunny\n"
                "2) Cloudy\n"
                "3) Light Rain\n"
                "4) Heavy Rain\n"
                "5) Not set"
            )
            return


        # ADD ACTION REQUIRED (initial text in REVIEW)
        if mode == MODE_ADD_ACTION_REQUIRED:
            try:
                num = int(context.user_data.get("new_action_number", 0) or 0)
            except Exception:
                num = 0

            _ensure_observation_defaults(session)
            _ensure_actions_defaults(session)

            item = _find_item_by_kind_number(session, "AR", num) if num else None
            if item is None:
                clear_mode(context)
                await update.message.reply_text("Cancelled.")
                return

            item["raw_text"] = text
            session_store.touch_updated_at(session)
            session_store.save_session_for_chat(chat_id, session)

            clear_mode(context)
            context.user_data.pop("new_action_number", None)
            await update.message.reply_text("Added.")
            return

        # ADD ACTION COMPLETED (initial text in REVIEW)
        if mode == MODE_ADD_ACTION_COMPLETED:
            try:
                num = int(context.user_data.get("new_action_number", 0) or 0)
            except Exception:
                num = 0

            _ensure_observation_defaults(session)
            _ensure_actions_defaults(session)

            item = _find_item_by_kind_number(session, "AC", num) if num else None
            if item is None:
                clear_mode(context)
                await update.message.reply_text("Cancelled.")
                return

            item["raw_text"] = text
            session_store.touch_updated_at(session)
            session_store.save_session_for_chat(chat_id, session)

            clear_mode(context)
            context.user_data.pop("new_action_number", None)
            await update.message.reply_text("Added.")
            return

        # EDIT_CATEGORY: choose o/ar/ac/rev
        if mode == MODE_EDIT_CATEGORY:
            cat_in = (text or "").strip().lower()
            if cat_in in ("o", "obs", "observation", "observations"):
                cat = "o"
            elif cat_in in ("ar", "action_required", "actionrequired"):
                cat = "ar"
            elif cat_in in ("ac", "action_completed", "actioncompleted"):
                cat = "ac"
            elif cat_in in ("rev", "r", "review"):
                cat = "rev"
            else:
                await update.message.reply_text("Reply one of: o / ar / ac / rev")
                return

            _ensure_observation_defaults(session)
            _ensure_actions_defaults(session)

            context.user_data["edit_category"] = cat
            edit_map: dict[str, tuple[str, int]] = {}

            lines = ["Select item number:"]

            if cat == "o":
                observations = session.get("observations", []) or []
                if not observations:
                    await update.message.reply_text("No observations.")
                    clear_mode(context)
                    return

                for obs in observations:
                    n = int(obs.get("number", 0) or 0)
                    preview = _truncate_one_line(obs.get("raw_text", ""))
                    phs = obs.get("photos", []) or []
                    ph_text = f" [{', '.join(phs)}]" if phs else ""
                    lines.append(f"{n}) {preview}{ph_text}")
                    edit_map[str(n)] = ("OBS", n)

            elif cat == "ar":
                items = session.get("actions_required", []) or []
                if not items:
                    await update.message.reply_text("No Action Required items.")
                    clear_mode(context)
                    return
                for it in items:
                    n = int(it.get("number", 0) or 0)
                    preview = _truncate_one_line(it.get("raw_text", ""))
                    phs = it.get("photos", []) or []
                    ph_text = f" [{', '.join(phs)}]" if phs else ""
                    lines.append(f"{n}) {preview}{ph_text}")
                    edit_map[str(n)] = ("AR", n)

            elif cat == "ac":
                items = session.get("actions_completed", []) or []
                if not items:
                    await update.message.reply_text("No Action Completed items.")
                    clear_mode(context)
                    return
                for it in items:
                    n = int(it.get("number", 0) or 0)
                    preview = _truncate_one_line(it.get("raw_text", ""))
                    phs = it.get("photos", []) or []
                    ph_text = f" [{', '.join(phs)}]" if phs else ""
                    lines.append(f"{n}) {preview}{ph_text}")
                    edit_map[str(n)] = ("AC", n)

            else:  # rev
                review_items = session.get("review_items", []) or []
                if not review_items:
                    await update.message.reply_text("No review items.")
                    clear_mode(context)
                    return
                for it in review_items:
                    n = int(it.get("number", 0) or 0)
                    preview = _truncate_one_line(it.get("text", ""))
                    lines.append(f"{n}) {preview}")
                    edit_map[str(n)] = ("REV", n)

            context.user_data["edit_map"] = edit_map

            set_mode(context, MODE_EDIT_SELECT)
            await update.message.reply_text("\n".join(lines))
            return

        # EDIT_SELECT: choose number within selected category
        if mode == MODE_EDIT_SELECT:
            edit_map = context.user_data.get("edit_map") or {}
            if not edit_map:
                clear_mode(context)
                await update.message.reply_text("Nothing to edit.")
                return

            try:
                pick = int(text)
            except ValueError:
                await update.message.reply_text("Please reply with a number.")
                return

            if str(pick) not in edit_map:
                await update.message.reply_text("Invalid number. Please try again.")
                return

            kind, ref_no = edit_map[str(pick)]
            context.user_data["edit_selected_kind"] = kind
            context.user_data["edit_selected_number"] = int(ref_no)

            # Set active target for photos (OBS/AR/AC only)
            if kind in ("OBS", "AR", "AC"):
                _set_active_target(session, kind, int(ref_no))
                session_store.touch_updated_at(session)
                session_store.save_session_for_chat(chat_id, session)

            set_mode(context, MODE_EDIT_MODE)
            await update.message.reply_text("Choose edit mode:\n1) Replace (overwrite)\n2) Append\nReply 1 or 2.")
            return

        # EDIT_MODE: choose 1 or 2
        if mode == MODE_EDIT_MODE:
            if text not in ("1", "2"):
                await update.message.reply_text("Reply 1 or 2.")
                return

            context.user_data["edit_mode_choice"] = int(text)
            set_mode(context, MODE_EDIT_TEXT)
            await update.message.reply_text("Send new text.")
            return

        # EDIT_TEXT: apply replace/append
        if mode == MODE_EDIT_TEXT:
            kind = context.user_data.get("edit_selected_kind")
            ref_no = context.user_data.get("edit_selected_number")
            choice = context.user_data.get("edit_mode_choice")

            if kind not in ("OBS", "AR", "AC", "REV") or ref_no is None or choice not in (1, 2):
                clear_mode(context)
                await update.message.reply_text("Cancelled.")
                return

            _ensure_observation_defaults(session)
            _ensure_actions_defaults(session)

            if kind in ("OBS", "AR", "AC"):
                item = _find_item_by_kind_number(session, kind, int(ref_no))
                if item is None:
                    clear_mode(context)
                    await update.message.reply_text("Invalid selection.")
                    return

                current = item.get("raw_text", "") or ""
                if choice == 1:
                    item["raw_text"] = text
                else:
                    item["raw_text"] = (current + "\n" + text) if current else text

            else:  # REV
                review_items = session.get("review_items", []) or []
                target = None
                for it in review_items:
                    if int(it.get("number", -1)) == int(ref_no):
                        target = it
                        break
                if target is None:
                    clear_mode(context)
                    await update.message.reply_text("Invalid selection.")
                    return

                current = target.get("text", "") or ""
                if choice == 1:
                    target["text"] = text
                else:
                    target["text"] = (current + "\n" + text) if current else text

            session_store.touch_updated_at(session)
            session_store.save_session_for_chat(chat_id, session)

            clear_mode(context)
            context.user_data.pop("edit_category", None)
            context.user_data.pop("edit_map", None)
            context.user_data.pop("edit_selected_kind", None)
            context.user_data.pop("edit_selected_number", None)
            context.user_data.pop("edit_mode_choice", None)

            await update.message.reply_text("Updated.")
            return


            if kind == "OBS":
                observations = session.get("observations", []) or []
                if index < 0 or index >= len(observations):
                    clear_mode(context)
                    await update.message.reply_text("Invalid selection.")
                    return

                current = observations[index].get("raw_text", "") or ""
                if choice == 1:
                    observations[index]["raw_text"] = text
                else:
                    observations[index]["raw_text"] = (current + "\n" + text) if current else text

            else:  # "REV"
                review_items = session.get("review_items", []) or []
                if index < 0 or index >= len(review_items):
                    clear_mode(context)
                    await update.message.reply_text("Invalid selection.")
                    return

                current = review_items[index].get("text", "") or ""
                if choice == 1:
                    review_items[index]["text"] = text
                else:
                    review_items[index]["text"] = (current + "\n" + text) if current else text

            session_store.touch_updated_at(session)
            session_store.save_session_for_chat(chat_id, session)

            clear_mode(context)
            context.user_data.pop("edit_selected_kind", None)
            context.user_data.pop("edit_selected_index", None)
            context.user_data.pop("edit_mode_choice", None)

            await update.message.reply_text("Updated.")
            return

        clear_mode(context)
        _clear_all_pending(context)
        await update.message.reply_text("Cancelled.")
        return

    # No MODE: normal behavior
    session = session_store.load_session_for_chat(chat_id)
    if not session:
        return

    # CAPTURING auto-append only
    if not status_is_capturing(session):
        return

    kind, item = _find_active_item(session)
    if item is None:
        await update.message.reply_text("No active inspection item. Use /new.")
        return

    existing = item.get("raw_text", "") or ""
    item["raw_text"] = (existing + "\n" + text) if existing else text

    session_store.touch_updated_at(session)
    session_store.save_session_for_chat(chat_id, session)
    return  # no reply for speed


# ----------------------------
# Photo handlers (CAPTURING only)
# ----------------------------
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.photo:
        return

    chat_id = update.effective_chat.id
    session = session_store.load_session_for_chat(chat_id)
    if not session:
        return

    if status_is_locked(session):
        return

    if not (status_is_capturing(session) or status_is_review(session)):
        return

    kind, item = _find_active_item(session)
    if item is None:
        await update.message.reply_text("No active item selected. Use /new (capture) or /edit (review).")
        return

    photo = update.message.photo[-1]
    photo_size = getattr(photo, "file_size", None)
    if photo_size is not None and int(photo_size) > PHOTO_MAX_BYTES:
        await update.message.reply_text("Photo too large. Please resend smaller image.")
        return

    counter = int(session.get("photo_counter", 1))
    ph_id = f"PH-{counter:03d}"
    filename = f"{ph_id}.jpg"

    try:
        tg_file = await photo.get_file()
        file_size = getattr(tg_file, "file_size", None)
        if file_size is not None and int(file_size) > PHOTO_MAX_BYTES:
            await update.message.reply_text("Photo too large. Please resend smaller image.")
            return

        photo_dir = ensure_tmp_photo_dir(str(session["inspection_id"]))
        target_path = photo_dir / filename
        await tg_file.download_to_drive(custom_path=str(target_path))

    except Exception as e:
        logger.exception("Photo download failed: %s", e)
        await update.message.reply_text("Photo save failed. Please resend.")
        return

    item.setdefault("photos", []).append(ph_id)
    session["photo_counter"] = counter + 1
    session_store.touch_updated_at(session)
    session_store.save_session_for_chat(chat_id, session)
    return  # no reply on success


async def on_document_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.document:
        return

    doc = update.message.document
    mime = (doc.mime_type or "").lower()
    if not mime.startswith("image/"):
        return

    chat_id = update.effective_chat.id
    session = session_store.load_session_for_chat(chat_id)
    if not session:
        return

    if status_is_locked(session):
        return

    if not (status_is_capturing(session) or status_is_review(session)):
        return

    kind, item = _find_active_item(session)
    if item is None:
        await update.message.reply_text("No active item selected. Use /new (capture) or /edit (review).")
        return

    doc_size = getattr(doc, "file_size", None)
    if doc_size is not None and int(doc_size) > PHOTO_MAX_BYTES:
        await update.message.reply_text("Photo too large. Please resend smaller image.")
        return

    counter = int(session.get("photo_counter", 1))
    ph_id = f"PH-{counter:03d}"
    filename = f"{ph_id}.jpg"

    try:
        tg_file = await doc.get_file()
        file_size = getattr(tg_file, "file_size", None)
        if file_size is not None and int(file_size) > PHOTO_MAX_BYTES:
            await update.message.reply_text("Photo too large. Please resend smaller image.")
            return

        photo_dir = ensure_tmp_photo_dir(str(session["inspection_id"]))
        target_path = photo_dir / filename
        await tg_file.download_to_drive(custom_path=str(target_path))

    except Exception as e:
        logger.exception("Document image download failed: %s", e)
        await update.message.reply_text("Photo save failed. Please resend.")
        return

    item.setdefault("photos", []).append(ph_id)
    session["photo_counter"] = counter + 1
    session_store.touch_updated_at(session)
    session_store.save_session_for_chat(chat_id, session)
    return  # no reply on success


def build_app():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN environment variable.")

    app = ApplicationBuilder().token(token).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("setups", cmd_setups))
    app.add_handler(CommandHandler("currentsetup", cmd_currentsetup))
    app.add_handler(CommandHandler("clearsetup", cmd_clearsetup))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("goto", cmd_goto))
    app.add_handler(CommandHandler("go", cmd_go))
    app.add_handler(CommandHandler("action_required", cmd_action_required))
    app.add_handler(CommandHandler("action_completed", cmd_action_completed))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("review", cmd_review))
    app.add_handler(CommandHandler("info", cmd_info))
    app.add_handler(CommandHandler("edit", cmd_edit))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("hide", cmd_hide))
    app.add_handler(CommandHandler("confirm", cmd_confirm))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # Photos
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, on_document_image))

    # Text (non-command)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    return app


def main() -> None:
    # Ensure all relative paths (including session_store internals) resolve under DATA_ROOT.
    os.chdir(DATA_ROOT)
    session_store.ensure_sessions_dir()
    TMP_PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    ensure_job_dirs()

    app = build_app()
    logger.info("Inspection Bot started (long polling).")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
