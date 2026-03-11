"""
Phase 3 – AI Rewrite Engine (Local, Mode 1 / Graceful Degrade)

Locked rules:
- A1: Rewrite ONLY observations where include_in_report == True
- Review items: ALWAYS rewrite (user confirmed)
- Action Required / Action Completed: ALWAYS rewrite (user confirmed)
- Mode 1: Never fail the export job; downstream falls back to raw/text
- Strategy 1 (C1): Rewrite once per (input + policy_version). If failed once, do not auto-retry.

This module NEVER modifies raw_text.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple

try:
    from openai import OpenAI  # type: ignore
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore


# ---------------------------
# Policy versions
# ---------------------------
POLICY_VERSION = "L2.v1"          # Observations + review items
ACTION_POLICY_VERSION = "A1.v1"   # Action Required + Action Completed

DEFAULT_PROVIDER = "openai"
DEFAULT_MODEL = "gpt-4.1-mini"
DEFAULT_TEMPERATURE = 0.1
DEFAULT_MAX_OUTPUT_TOKENS = 450

# ---------------------------
# Prompts
# ---------------------------
SYSTEM_MESSAGE = """You are a professional engineering report editor.

Your task is to rewrite field observation notes into formal engineering report language.

Strict rules:

Do NOT add new facts, technical details, quantities, locations, materials, or standards not explicitly stated.
Do NOT omit any technical detail, condition, or observation stated in the original note.
Do NOT infer compliance, non-compliance, risk level, conclusions, or intent.
Do NOT add recommendations, corrective actions, or suggestions.
Preserve any uncertainty (e.g., "appears", "possibly", "may", "uncertain").
Do NOT strengthen or weaken the level of certainty or severity.
Preserve original technical terminology unless grammatical correction is required.
Grammar and clarity may be improved while preserving the original meaning.
Keep the meaning exactly the same as the original note.
Use neutral engineering report tone and objective phrasing.

Output requirements:
Output only a single paragraph.
Do not include numbering, headings, bullet points, or labels.
Do not explain your reasoning.
Do not restate the instructions.
If the original note contains ambiguous or incomplete information, maintain that ambiguity without clarification.

Your output must be neutral, factual, and professionally written.
"""

USER_TEMPLATE = (
    "Rewrite the following field observation into formal engineering report style:\n\n"
    "---\n"
    "{raw_text}\n"
    "---\n\n"
    "Return only the rewritten paragraph."
)

ACTION_SYSTEM_MESSAGE = """You are a professional engineering report editor.

Your task is to rewrite contractor action notes into formal, report-ready English.

Strict rules:

Do NOT add any new facts (including quantities, dimensions, locations, materials, dates, standards, causes, or parties) not explicitly stated.
Do NOT add any additional actions beyond what is explicitly stated.
Do NOT infer or introduce compliance, non-compliance, risk, judgement, conclusions, or recommendations.
You MAY add an explicit subject (e.g., "The contractor") only to improve clarity, without changing meaning.
Preserve uncertainty and conditional wording exactly as written (e.g., "may", "appears", "to be confirmed").

Output requirements:
Output only a single paragraph.
Do not include numbering, headings, bullet points, or labels.
Do not explain your reasoning.
Do not restate the instructions.
"""

ACTION_REQUIRED_USER_TEMPLATE = (
    "Rewrite the following Action Required note into formal engineering report language.\n"
    "Tone requirement: the wording must clearly indicate a required action to be carried out, "
    "without introducing new facts or additional actions.\n\n"
    "---\n"
    "{raw_text}\n"
    "---\n\n"
    "Return only the rewritten paragraph."
)

ACTION_COMPLETED_USER_TEMPLATE = (
    "Rewrite the following Action Completed note into formal engineering report language.\n"
    "Tone requirement: the wording must clearly indicate the action has been completed (past tense), "
    "without introducing new facts or additional actions.\n\n"
    "---\n"
    "{raw_text}\n"
    "---\n\n"
    "Return only the rewritten paragraph."
)


@dataclass(frozen=True)
class RewriteConfig:
    policy_version: str = POLICY_VERSION
    provider: str = DEFAULT_PROVIDER
    model: str = DEFAULT_MODEL
    temperature: float = DEFAULT_TEMPERATURE
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_input_hash(raw_text: str, policy_version: str) -> str:
    s = f"{raw_text}\n{policy_version}".encode("utf-8")
    return "sha256:" + hashlib.sha256(s).hexdigest()


def compute_output_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


_BULLET_RE = re.compile(r"^\s*([-*•]|\d+[.)])\s+", re.MULTILINE)


def _is_valid_single_paragraph(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if _BULLET_RE.search(t):
        return False
    if re.search(r"^\s*(risk|recommendation|conclusion)\s*[:\-]", t, re.IGNORECASE | re.MULTILINE):
        return False
    if "\n\n" in t:
        return False
    return True


def _json_sanitize(value: Any) -> Any:
    """Convert arbitrary objects to JSON-serializable primitives."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_sanitize(v) for v in value]
    if hasattr(value, "model_dump"):
        try:
            return _json_sanitize(value.model_dump())  # type: ignore[attr-defined]
        except Exception:
            pass
    return str(value)


def _extract_usage_minimal(usage: Any) -> Optional[Dict[str, Any]]:
    """
    Keep only stable, JSON-safe fields.
    The OpenAI SDK may include nested classes (e.g., InputTokensDetails) which break json.dumps.
    """
    if usage is None:
        return None

    if hasattr(usage, "model_dump"):
        try:
            return _json_sanitize(usage.model_dump())  # type: ignore[attr-defined]
        except Exception:
            pass

    try:
        return _json_sanitize(dict(usage))  # type: ignore[arg-type]
    except Exception:
        pass

    out: Dict[str, Any] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        if hasattr(usage, key):
            out[key] = _json_sanitize(getattr(usage, key))
    return out or None


def should_rewrite_text(
    *,
    raw_text: str,
    rewritten_text: str,
    meta: Dict[str, Any] | None,
    policy_version: str,
) -> bool:
    """Strategy 1 (C1): decide whether to rewrite based on (raw_text + policy_version)."""
    raw_text = (raw_text or "").strip()
    if not raw_text:
        return False

    meta = meta or {}
    rewritten_text = (rewritten_text or "").strip()
    current_input_hash = compute_input_hash(raw_text, policy_version)

    if not meta or not meta.get("attempted_at"):
        return True
    if meta.get("policy_version") != policy_version:
        return True
    if meta.get("input_hash") != current_input_hash:
        return True
    if meta.get("success") is True and rewritten_text:
        return False
    if meta.get("success") is False and meta.get("input_hash") == current_input_hash:
        return False
    if meta.get("success") is True and not rewritten_text:
        return True
    return True


def _get_openai_client(passed_client: Any = None) -> Any:
    if passed_client is not None:
        return passed_client
    if OpenAI is None:
        raise RuntimeError("OpenAI SDK not installed in this environment")
    return OpenAI()


def rewrite_one_text(
    raw_text: str,
    *,
    config: RewriteConfig,
    client: Any = None,
    system_message: str,
    user_template: str,
) -> Tuple[Optional[str], Dict[str, Any]]:
    """
    Generic rewrite call.
    Returns (rewritten_text_or_none, meta).
    """
    started = time.time()

    meta: Dict[str, Any] = {
        "policy_version": config.policy_version,
        "provider": config.provider,
        "model": config.model,
        "attempted_at": _now_iso(),
        "success": False,
        "error": None,
        "error_detail": None,
        "input_hash": compute_input_hash(raw_text, config.policy_version),
        "output_hash": None,
        "temperature": config.temperature,
        "latency_ms": None,
        "usage": None,
        "request_id": None,
    }

    if config.provider == "openai" and not (os.environ.get("OPENAI_API_KEY") or "").strip():
        meta["error"] = "missing_api_key"
        meta["latency_ms"] = int((time.time() - started) * 1000)
        return None, meta

    user_input = user_template.format(raw_text=raw_text)

    try:
        c = _get_openai_client(client)

        resp = c.responses.create(
            model=config.model,
            instructions=system_message,
            input=user_input,
            temperature=config.temperature,
            max_output_tokens=config.max_output_tokens,
        )

        output_text = (getattr(resp, "output_text", "") or "").strip()

        meta["request_id"] = _json_sanitize(getattr(resp, "id", None))
        meta["usage"] = _extract_usage_minimal(getattr(resp, "usage", None))

        if not _is_valid_single_paragraph(output_text):
            meta["error"] = "invalid_output"
            meta["latency_ms"] = int((time.time() - started) * 1000)
            return None, meta

        meta["success"] = True
        meta["output_hash"] = compute_output_hash(output_text)
        meta["latency_ms"] = int((time.time() - started) * 1000)
        return output_text, meta

    except Exception as e:
        meta["error"] = type(e).__name__
        meta["error_detail"] = str(e)[:500]
        meta["latency_ms"] = int((time.time() - started) * 1000)
        return None, meta


def _bump_reason(fail_reasons: Dict[str, int], reason: str) -> None:
    reason = str(reason or "unknown")
    fail_reasons[reason] = fail_reasons.get(reason, 0) + 1


def _section_summary() -> Dict[str, Any]:
    return {"attempted": 0, "rewritten": 0, "skipped": 0, "failed": 0, "fail_reasons": {}}


def rewrite_session_if_needed(
    session: Dict[str, Any],
    *,
    save_checkpoint: Callable[[Dict[str, Any]], None],
    client: Any = None,
    config: RewriteConfig = RewriteConfig(),
) -> Dict[str, Any]:
    """
    Rewrites:
    - observations[].raw_text -> observations[].rewritten_text (A1 gate)
    - review_items[].text -> review_items[].rewritten_text (always rewrite)
    - actions_required[].raw_text -> actions_required[].rewritten_text (always rewrite)
    - actions_completed[].raw_text -> actions_completed[].rewritten_text (always rewrite)

    Notes:
    - Never modifies raw_text.
    - Strategy 1 (C1) is applied per-item via rewrite_meta + (raw_text + policy_version) hash.
    """
    observations = session.get("observations")
    if not isinstance(observations, list):
        raise ValueError("session.observations must be a list")

    review_items = session.get("review_items") or []
    if not isinstance(review_items, list):
        raise ValueError("session.review_items must be a list")

    actions_required = session.get("actions_required") or []
    if not isinstance(actions_required, list):
        raise ValueError("session.actions_required must be a list")

    actions_completed = session.get("actions_completed") or []
    if not isinstance(actions_completed, list):
        raise ValueError("session.actions_completed must be a list")

    # Action config inherits model/provider/temperature/token limit, but uses action policy version.
    action_config = RewriteConfig(
        policy_version=ACTION_POLICY_VERSION,
        provider=config.provider,
        model=config.model,
        temperature=config.temperature,
        max_output_tokens=config.max_output_tokens,
    )

    sec_obs = _section_summary()
    sec_rev = _section_summary()
    sec_ar = _section_summary()
    sec_ac = _section_summary()

    # ---- Observations (A1) ----
    for obs in observations:
        if not isinstance(obs, dict):
            continue

        if obs.get("include_in_report") is not True:
            sec_obs["skipped"] += 1
            continue

        raw_text = (obs.get("raw_text") or "").strip()
        if not raw_text:
            sec_obs["skipped"] += 1
            continue

        meta = obs.get("rewrite_meta") or {}
        existing = (obs.get("rewritten_text") or "").strip()

        if not should_rewrite_text(
            raw_text=raw_text,
            rewritten_text=existing,
            meta=meta,
            policy_version=config.policy_version,
        ):
            sec_obs["skipped"] += 1
            continue

        sec_obs["attempted"] += 1
        rewritten_text, meta2 = rewrite_one_text(
            raw_text,
            config=config,
            client=client,
            system_message=SYSTEM_MESSAGE,
            user_template=USER_TEMPLATE,
        )
        meta2 = _json_sanitize(meta2)

        obs["rewrite_meta"] = meta2
        if meta2.get("success") is True and rewritten_text:
            obs["rewritten_text"] = rewritten_text
            sec_obs["rewritten"] += 1
        else:
            obs["rewritten_text"] = None
            sec_obs["failed"] += 1
            _bump_reason(sec_obs["fail_reasons"], str(meta2.get("error") or "unknown"))

        save_checkpoint(session)

    # ---- Review items (always rewrite; observation policy) ----
    for item in review_items:
        if not isinstance(item, dict):
            continue

        raw_text = (item.get("text") or "").strip()
        if not raw_text:
            sec_rev["skipped"] += 1
            continue

        meta = item.get("rewrite_meta") or {}
        existing = (item.get("rewritten_text") or "").strip()

        if not should_rewrite_text(
            raw_text=raw_text,
            rewritten_text=existing,
            meta=meta,
            policy_version=config.policy_version,
        ):
            sec_rev["skipped"] += 1
            continue

        sec_rev["attempted"] += 1
        rewritten_text, meta2 = rewrite_one_text(
            raw_text,
            config=config,
            client=client,
            system_message=SYSTEM_MESSAGE,
            user_template=USER_TEMPLATE,
        )
        meta2 = _json_sanitize(meta2)

        item["rewrite_meta"] = meta2
        if meta2.get("success") is True and rewritten_text:
            item["rewritten_text"] = rewritten_text
            sec_rev["rewritten"] += 1
        else:
            item["rewritten_text"] = None
            sec_rev["failed"] += 1
            _bump_reason(sec_rev["fail_reasons"], str(meta2.get("error") or "unknown"))

        save_checkpoint(session)

    # ---- Action Required (always rewrite; action policy) ----
    for it in actions_required:
        if not isinstance(it, dict):
            continue

        raw_text = (it.get("raw_text") or "").strip()
        if not raw_text:
            sec_ar["skipped"] += 1
            continue

        meta = it.get("rewrite_meta") or {}
        existing = (it.get("rewritten_text") or "").strip()

        if not should_rewrite_text(
            raw_text=raw_text,
            rewritten_text=existing,
            meta=meta,
            policy_version=action_config.policy_version,
        ):
            sec_ar["skipped"] += 1
            continue

        sec_ar["attempted"] += 1
        rewritten_text, meta2 = rewrite_one_text(
            raw_text,
            config=action_config,
            client=client,
            system_message=ACTION_SYSTEM_MESSAGE,
            user_template=ACTION_REQUIRED_USER_TEMPLATE,
        )
        meta2 = _json_sanitize(meta2)

        it["rewrite_meta"] = meta2
        if meta2.get("success") is True and rewritten_text:
            it["rewritten_text"] = rewritten_text
            sec_ar["rewritten"] += 1
        else:
            it["rewritten_text"] = None
            sec_ar["failed"] += 1
            _bump_reason(sec_ar["fail_reasons"], str(meta2.get("error") or "unknown"))

        save_checkpoint(session)

    # ---- Action Completed (always rewrite; action policy) ----
    for it in actions_completed:
        if not isinstance(it, dict):
            continue

        raw_text = (it.get("raw_text") or "").strip()
        if not raw_text:
            sec_ac["skipped"] += 1
            continue

        meta = it.get("rewrite_meta") or {}
        existing = (it.get("rewritten_text") or "").strip()

        if not should_rewrite_text(
            raw_text=raw_text,
            rewritten_text=existing,
            meta=meta,
            policy_version=action_config.policy_version,
        ):
            sec_ac["skipped"] += 1
            continue

        sec_ac["attempted"] += 1
        rewritten_text, meta2 = rewrite_one_text(
            raw_text,
            config=action_config,
            client=client,
            system_message=ACTION_SYSTEM_MESSAGE,
            user_template=ACTION_COMPLETED_USER_TEMPLATE,
        )
        meta2 = _json_sanitize(meta2)

        it["rewrite_meta"] = meta2
        if meta2.get("success") is True and rewritten_text:
            it["rewritten_text"] = rewritten_text
            sec_ac["rewritten"] += 1
        else:
            it["rewritten_text"] = None
            sec_ac["failed"] += 1
            _bump_reason(sec_ac["fail_reasons"], str(meta2.get("error") or "unknown"))

        save_checkpoint(session)

    # ---- Totals ----
    def _sum_sections(*secs: Dict[str, Any]) -> Dict[str, Any]:
        total = {"attempted": 0, "rewritten": 0, "skipped": 0, "failed": 0, "fail_reasons": {}}
        for s in secs:
            total["attempted"] += int(s.get("attempted", 0) or 0)
            total["rewritten"] += int(s.get("rewritten", 0) or 0)
            total["skipped"] += int(s.get("skipped", 0) or 0)
            total["failed"] += int(s.get("failed", 0) or 0)
            fr = s.get("fail_reasons") or {}
            if isinstance(fr, dict):
                for k, v in fr.items():
                    total["fail_reasons"][str(k)] = total["fail_reasons"].get(str(k), 0) + int(v or 0)
        return total

    total = _sum_sections(sec_obs, sec_rev, sec_ar, sec_ac)

    return {
        "policies": {
            "observations_and_review_items": {
                "policy_version": config.policy_version,
                "provider": config.provider,
                "model": config.model,
                "temperature": config.temperature,
                "max_output_tokens": config.max_output_tokens,
            },
            "actions": {
                "policy_version": action_config.policy_version,
                "provider": action_config.provider,
                "model": action_config.model,
                "temperature": action_config.temperature,
                "max_output_tokens": action_config.max_output_tokens,
            },
        },
        "observations": sec_obs,
        "review_items": sec_rev,
        "actions_required": sec_ar,
        "actions_completed": sec_ac,
        "total": total,
    }
