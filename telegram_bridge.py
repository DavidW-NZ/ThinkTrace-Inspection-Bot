import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen
from uuid import uuid4


DEFAULT_TIMEOUT_SECONDS = 10.0


class TelegramBridgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class TelegramBridgeConfig:
    base_url: str
    token: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


@dataclass(frozen=True)
class InspectionSetupSummary:
    setup_id: str
    setup_name: str
    project_id: str
    selected_template_id: str | None
    is_active: bool


@dataclass(frozen=True)
class InspectionOutputWriteConfig:
    base_url: str
    token: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


def _load_bridge_base_url_from_env() -> str:
    return (
        (os.environ.get("THINKTRACE_BRIDGE_BASE_URL") or "").strip()
        or (os.environ.get("THINKTRACE_BASE_URL") or "").strip()
    )


def load_telegram_bridge_config_from_env() -> TelegramBridgeConfig | None:
    base_url = _load_bridge_base_url_from_env()
    token = (os.environ.get("TELEGRAM_BRIDGE_TOKEN") or "").strip()

    if not base_url and not token:
        return None
    if not base_url or not token:
        raise TelegramBridgeError(
            "Both THINKTRACE_BASE_URL/THINKTRACE_BRIDGE_BASE_URL and TELEGRAM_BRIDGE_TOKEN are required."
        )

    return TelegramBridgeConfig(base_url=base_url.rstrip("/") + "/", token=token)


def load_inspection_output_write_config_from_env() -> InspectionOutputWriteConfig | None:
    base_url = _load_bridge_base_url_from_env()
    token = (os.environ.get("TELEGRAM_INSPECTION_OUTPUT_WRITE_TOKEN") or "").strip()

    if not base_url and not token:
        return None
    if not base_url or not token:
        raise TelegramBridgeError(
            "Both THINKTRACE_BASE_URL/THINKTRACE_BRIDGE_BASE_URL and TELEGRAM_INSPECTION_OUTPUT_WRITE_TOKEN are required."
        )

    return InspectionOutputWriteConfig(base_url=base_url.rstrip("/") + "/", token=token)


def fetch_inspection_setups(
    telegram_user_id: int,
    *,
    active_only: bool = True,
    config: TelegramBridgeConfig | None = None,
) -> list[InspectionSetupSummary]:
    if int(telegram_user_id) <= 0:
        raise TelegramBridgeError("telegram_user_id must be a positive integer.")

    cfg = config or load_telegram_bridge_config_from_env()
    if cfg is None:
        raise TelegramBridgeError(
            "Telegram bridge is not configured. Missing THINKTRACE_BASE_URL/THINKTRACE_BRIDGE_BASE_URL and TELEGRAM_BRIDGE_TOKEN."
        )

    query = urlencode(
        {
            "telegram_user_id": int(telegram_user_id),
            "active_only": "true" if active_only else "false",
        }
    )
    url = urljoin(cfg.base_url, "telegram/inspection-setups") + f"?{query}"
    request = Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "X-Telegram-Bridge-Token": cfg.token,
        },
    )

    try:
        with urlopen(request, timeout=cfg.timeout_seconds) as response:
            payload = json.load(response)
    except Exception as exc:
        raise TelegramBridgeError("Failed to fetch inspection setups from ThinkTrace bridge.") from exc

    if not isinstance(payload, dict):
        raise TelegramBridgeError("ThinkTrace bridge returned an invalid inspection setups payload.")

    if payload.get("success") is not True:
        raise TelegramBridgeError("ThinkTrace bridge reported an unsuccessful inspection setups response.")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise TelegramBridgeError("ThinkTrace bridge returned an invalid inspection setups data envelope.")

    items = data.get("items")
    if not isinstance(items, list):
        raise TelegramBridgeError("ThinkTrace bridge returned an invalid inspection setups items list.")

    return [_parse_inspection_setup(item) for item in items]


def write_inspection_output(
    *,
    file_name: str,
    content_type: str,
    output_bytes: bytes,
    metadata: dict[str, Any],
    config: InspectionOutputWriteConfig | None = None,
) -> None:
    if not file_name.strip():
        raise TelegramBridgeError("file_name is required.")
    if not content_type.strip():
        raise TelegramBridgeError("content_type is required.")
    if not isinstance(output_bytes, bytes) or not output_bytes:
        raise TelegramBridgeError("output_bytes must be non-empty bytes.")
    if not isinstance(metadata, dict) or not metadata:
        raise TelegramBridgeError("metadata must be a non-empty object.")

    cfg = config or load_inspection_output_write_config_from_env()
    if cfg is None:
        raise TelegramBridgeError(
            "Inspection output write bridge is not configured. Missing THINKTRACE_BASE_URL/THINKTRACE_BRIDGE_BASE_URL and TELEGRAM_INSPECTION_OUTPUT_WRITE_TOKEN."
        )

    body, boundary = _encode_multipart_form_data(
        metadata=metadata,
        file_name=file_name,
        content_type=content_type,
        output_bytes=output_bytes,
    )
    url = urljoin(cfg.base_url, "telegram/inspection-outputs")
    request = Request(
        url,
        method="POST",
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "X-Telegram-Inspection-Output-Write-Token": cfg.token,
        },
    )

    try:
        with urlopen(request, timeout=cfg.timeout_seconds) as response:
            payload = json.load(response)
    except Exception as exc:
        raise TelegramBridgeError("Failed to write inspection output to ThinkTrace bridge.") from exc

    if not isinstance(payload, dict):
        raise TelegramBridgeError("ThinkTrace bridge returned an invalid inspection output write payload.")
    if payload.get("success") is not True:
        raise TelegramBridgeError("ThinkTrace bridge reported an unsuccessful inspection output write response.")


def _encode_multipart_form_data(
    *,
    metadata: dict[str, Any],
    file_name: str,
    content_type: str,
    output_bytes: bytes,
) -> tuple[bytes, str]:
    boundary = f"thinktrace-{uuid4().hex}"
    lines = [
        f"--{boundary}\r\n".encode("utf-8"),
        b'Content-Disposition: form-data; name="metadata"\r\n',
        b"Content-Type: application/json; charset=utf-8\r\n\r\n",
        json.dumps(metadata, ensure_ascii=False).encode("utf-8"),
        b"\r\n",
        f"--{boundary}\r\n".encode("utf-8"),
        (
            f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'
        ).encode("utf-8"),
        f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
        output_bytes,
        b"\r\n",
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    return b"".join(lines), boundary


def _parse_inspection_setup(item: Any) -> InspectionSetupSummary:
    if not isinstance(item, dict):
        raise TelegramBridgeError("Inspection setup entries must be objects.")

    try:
        setup_id = str(item["setup_id"])
        setup_name = str(item["setup_name"])
        project_id = str(item["project_id"])
        template_id = item.get("selected_template_id")
        is_active = bool(item["is_active"])
    except KeyError as exc:
        raise TelegramBridgeError(f"Inspection setup entry is missing field: {exc.args[0]}") from exc

    return InspectionSetupSummary(
        setup_id=setup_id,
        setup_name=setup_name,
        project_id=project_id,
        selected_template_id=None if template_id is None else str(template_id),
        is_active=is_active,
    )
