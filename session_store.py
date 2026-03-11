import json
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, Optional

SESSIONS_DIR = Path("sessions")
ACTIVE_MAP_PATH = SESSIONS_DIR / "active_sessions.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_sessions_dir() -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def _tmp_path_for(path: Path) -> Path:
    # Keep temp file in the same directory so os.replace() is atomic on the same filesystem.
    return path.with_name(path.name + ".tmp")


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomically write text to `path`.

    Guarantees that either the old file remains intact, or the new file is fully written.
    """
    ensure_sessions_dir()
    tmp_path = _tmp_path_for(path)

    with tmp_path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp_path, path)


def _recover_or_cleanup_tmp(path: Path) -> None:
    """Recover from or clean up a leftover .tmp file for a given target path."""
    tmp_path = _tmp_path_for(path)
    if not tmp_path.exists():
        return

    if not path.exists():
        # No final file exists; promote tmp to final.
        os.replace(tmp_path, path)
    else:
        # Final exists; tmp is leftover.
        try:
            tmp_path.unlink()
        except OSError:
            pass


def _quarantine_corrupt_file(path: Path, suffix_label: str) -> Path:
    """Rename a corrupt file to preserve evidence for debugging/audit."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    corrupt_path = path.with_name(f"{path.name}.corrupt.{suffix_label}.{ts}")
    try:
        os.replace(path, corrupt_path)
    except OSError:
        return path
    return corrupt_path


# ----------------------------
# Active session mapping
# ----------------------------
def load_active_map() -> Dict[str, Any]:
    ensure_sessions_dir()
    _recover_or_cleanup_tmp(ACTIVE_MAP_PATH)

    if not ACTIVE_MAP_PATH.exists():
        return {"active_by_chat": {}}

    try:
        return json.loads(ACTIVE_MAP_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        _quarantine_corrupt_file(ACTIVE_MAP_PATH, "active_map")
        return {"active_by_chat": {}}


def save_active_map(data: Dict[str, Any]) -> None:
    ensure_sessions_dir()
    text = json.dumps(data, ensure_ascii=False, indent=2)
    _atomic_write_text(ACTIVE_MAP_PATH, text)


def get_active_inspection_id(chat_id: int) -> Optional[str]:
    data = load_active_map()
    return data.get("active_by_chat", {}).get(str(chat_id))


def set_active_inspection_id(chat_id: int, inspection_id: str) -> None:
    data = load_active_map()
    data.setdefault("active_by_chat", {})[str(chat_id)] = inspection_id
    save_active_map(data)


def clear_active_inspection_id(chat_id: int) -> None:
    data = load_active_map()
    if "active_by_chat" in data and str(chat_id) in data["active_by_chat"]:
        del data["active_by_chat"][str(chat_id)]
        save_active_map(data)


# ----------------------------
# Session JSON read/write
# ----------------------------
def session_path(inspection_id: str) -> Path:
    ensure_sessions_dir()
    return SESSIONS_DIR / f"{inspection_id}.json"


def load_session(inspection_id: str) -> Optional[Dict[str, Any]]:
    path = session_path(inspection_id)
    _recover_or_cleanup_tmp(path)

    if not path.exists():
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _quarantine_corrupt_file(path, "session")
        raise ValueError(f"Session JSON is corrupt: {path}") from e


def save_session(session: Dict[str, Any]) -> None:
    inspection_id = session.get("inspection_id")
    if not inspection_id:
        raise ValueError("session missing inspection_id")

    path = session_path(str(inspection_id))
    text = json.dumps(session, ensure_ascii=False, indent=2)
    _atomic_write_text(path, text)


def touch_updated_at(session: Dict[str, Any]) -> None:
    session["updated_at"] = _utc_now_iso()


def load_session_for_chat(chat_id: int) -> Optional[Dict[str, Any]]:
    insp_id = get_active_inspection_id(chat_id)
    if not insp_id:
        return None
    return load_session(insp_id)


def save_session_for_chat(chat_id: int, session: Dict[str, Any]) -> None:
    insp_id = session.get("inspection_id")
    if insp_id:
        set_active_inspection_id(chat_id, str(insp_id))
    save_session(session)
