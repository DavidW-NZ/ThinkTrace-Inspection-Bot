import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import List, Tuple, Dict, Any


LOCAL_TZ = ZoneInfo("Pacific/Auckland")


class ExportError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def normalize_text(s: str) -> str:
    if not isinstance(s, str):
        return ""
    lines = s.strip().splitlines()
    return "\n".join(line.rstrip() for line in lines)


def ensure_final_newline(text: str) -> str:
    if not text.endswith("\n"):
        return text + "\n"
    return text


def format_datetime_local(dt_str: str) -> str:
    if not dt_str:
        return ""
    try:
        s = str(dt_str).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(LOCAL_TZ)
        return dt.strftime("%d-%m-%Y %H:%M")
    except Exception:
        return ""


def build_export_text(session: Dict[str, Any]) -> str:
    if not isinstance(session, dict):
        raise ExportError("INVALID_SESSION", "Session must be dict.")

    if session.get("status") != "LOCKED":
        raise ExportError("NOT_LOCKED", "Session must be LOCKED for export.")

    required_keys = [
        "project_id",
        "inspection_id",
        "created_at",
        "observations",
        "review_items",
        "header",
    ]

    for key in required_keys:
        if key not in session:
            raise ExportError("MISSING_FIELD", f"Missing required field: {key}")

    project_id = session.get("project_id", "")
    inspection_id = session.get("inspection_id", "")
    created_at = format_datetime_local(session.get("created_at", ""))
    confirmed_at = format_datetime_local(session.get("confirmed_at", ""))

    header = session.get("header", {})
    title = header.get("title", "")
    location = header.get("location_text", "")
    weather = header.get("weather", "")
    datetime_override = header.get("datetime_override", "")

    observations = session.get("observations", [])
    review_items = session.get("review_items", [])

    # NEW (non-AI): Actions
    actions_required = session.get("actions_required", []) or []
    actions_completed = session.get("actions_completed", []) or []

    if not isinstance(observations, list):
        raise ExportError("INVALID_TYPE", "observations must be list.")

    if not isinstance(review_items, list):
        raise ExportError("INVALID_TYPE", "review_items must be list.")

    if not isinstance(actions_required, list):
        raise ExportError("INVALID_TYPE", "actions_required must be list.")

    if not isinstance(actions_completed, list):
        raise ExportError("INVALID_TYPE", "actions_completed must be list.")

    lines: List[str] = []

    # ===== Metadata =====
    lines.append("Export Spec: v1")
    lines.append("")
    lines.append(f"Project: {project_id}")
    lines.append(f"Inspection ID: {inspection_id}")
    lines.append("Status: LOCKED")
    lines.append(f"Created: {created_at}")
    lines.append(f"Confirmed: {confirmed_at}")
    lines.append("")
    lines.append(f"Title: {title}")
    lines.append(f"Location: {location}")
    lines.append(f"Weather: {weather}")
    lines.append(f"Datetime Override: {datetime_override}")
    lines.append("")
    lines.append("Construction Observations")
    lines.append("")

    # ===== Observations =====
    all_obs: List[Tuple[int, Dict[str, Any], bool]] = []
    report_obs: List[Tuple[int, Dict[str, Any]]] = []

    for obs in observations:
        n = obs.get("number")
        if not isinstance(n, int):
            raise ExportError("INVALID_OBSERVATION_NUMBER", "Observation number must be int.")

        include_flag = obs.get("include_in_report", True)

        all_obs.append((n, obs, include_flag))

        if include_flag:
            report_obs.append((n, obs))

    all_obs.sort(key=lambda x: x[0])
    report_obs.sort(key=lambda x: x[0])

    for n, obs, include_flag in all_obs:
        raw_text = normalize_text(obs.get("raw_text", "") or "")

        if raw_text:
            lines.append(f"{n}. {raw_text}")
        else:
            lines.append(f"{n}.")

        # ---- Always process Photo(s) ----
        ph_list = []
        for ph in obs.get("photos", []):
            if not isinstance(ph, str):
                raise ExportError("INVALID_PHOTO_ID", "Photo ID must be string.")
            if not re.match(r"^PH-\d{3}$", ph):
                raise ExportError("INVALID_PHOTO_ID", f"Invalid photo ID: {ph}")
            ph_list.append(ph)

        if ph_list:
            lines.append(f"Photo(s): {', '.join(ph_list)}")

        # ---- Hidden marker ----
        if not include_flag:
            lines.append("[HIDDEN – excluded from report]")

        lines.append("")

    # ===== Action Required =====
    lines.append("Action Required")
    lines.append("")

    if actions_required:
        sorted_ar = sorted(actions_required, key=lambda x: x.get("number", 0))
        for it in sorted_ar:
            num = it.get("number")
            if not isinstance(num, int):
                raise ExportError("INVALID_ACTION_NUMBER", "Action number must be int.")

            raw_text = normalize_text(it.get("raw_text", "") or "")
            lines.append(f"{num}. {raw_text}" if raw_text else f"{num}.")

            ph_list = []
            for ph in it.get("photos", []) or []:
                if not isinstance(ph, str):
                    raise ExportError("INVALID_PHOTO_ID", "Photo ID must be string.")
                if not re.match(r"^PH-\d{3}$", ph):
                    raise ExportError("INVALID_PHOTO_ID", f"Invalid photo ID: {ph}")
                ph_list.append(ph)
            if ph_list:
                lines.append(f"Photo(s): {', '.join(ph_list)}")

            lines.append("")
    else:
        lines.append("None")
        lines.append("")

    # ===== Action Completed =====
    lines.append("Action Completed")
    lines.append("")

    if actions_completed:
        sorted_ac = sorted(actions_completed, key=lambda x: x.get("number", 0))
        for it in sorted_ac:
            num = it.get("number")
            if not isinstance(num, int):
                raise ExportError("INVALID_ACTION_NUMBER", "Action number must be int.")

            raw_text = normalize_text(it.get("raw_text", "") or "")
            lines.append(f"{num}. {raw_text}" if raw_text else f"{num}.")

            ph_list = []
            for ph in it.get("photos", []) or []:
                if not isinstance(ph, str):
                    raise ExportError("INVALID_PHOTO_ID", "Photo ID must be string.")
                if not re.match(r"^PH-\d{3}$", ph):
                    raise ExportError("INVALID_PHOTO_ID", f"Invalid photo ID: {ph}")
                ph_list.append(ph)
            if ph_list:
                lines.append(f"Photo(s): {', '.join(ph_list)}")

            lines.append("")
    else:
        lines.append("None")
        lines.append("")

    # ===== Additional Review Items =====
    lines.append("Additional Review Items")
    lines.append("")

    if review_items:
        sorted_review = sorted(
            review_items,
            key=lambda x: x.get("number", 0)
        )

        for item in sorted_review:
            num = item.get("number")
            text = normalize_text(item.get("text", "") or "")
            lines.append(f"{num}. {text}")
    else:
        lines.append("None")

    lines.append("")
    lines.append("Photo Appendix")
    lines.append("")

    # ===== Photo Appendix (non-hidden only) =====
    photo_ids = set()

    for _, obs in report_obs:
        for ph in obs.get("photos", []):
            if not isinstance(ph, str):
                raise ExportError("INVALID_PHOTO_ID", "Photo ID must be string.")
            if not re.match(r"^PH-\d{3}$", ph):
                raise ExportError("INVALID_PHOTO_ID", f"Invalid photo ID: {ph}")
            photo_ids.add(ph)

    if photo_ids:
        sorted_ph = sorted(photo_ids, key=lambda x: int(x.split("-")[1]))
        for ph in sorted_ph:
            lines.append(ph)
    else:
        lines.append("None")

    output = "\n".join(lines)
    return ensure_final_newline(output)