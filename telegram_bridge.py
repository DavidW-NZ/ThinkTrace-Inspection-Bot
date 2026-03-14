import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen


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
    if not cfg.token.strip():
        raise TelegramBridgeError(
            "Inspection output write bridge is not configured. Missing TELEGRAM_INSPECTION_OUTPUT_WRITE_TOKEN."
        )

    url = urljoin(cfg.base_url, "telegram/inspection-outputs")

    try:
        response_body = _post_inspection_output_with_curl(
            url=url,
            token=cfg.token,
            timeout_seconds=cfg.timeout_seconds,
            metadata=metadata,
            file_name=file_name,
            content_type=content_type,
            output_bytes=output_bytes,
        )
        payload = json.loads(response_body)
    except TelegramBridgeError:
        raise
    except Exception as exc:
        raise TelegramBridgeError("Failed to write inspection output to ThinkTrace bridge.") from exc

    if not isinstance(payload, dict):
        raise TelegramBridgeError("ThinkTrace bridge returned an invalid inspection output write payload.")
    if payload.get("success") is not True:
        raise TelegramBridgeError("ThinkTrace bridge reported an unsuccessful inspection output write response.")


def _post_inspection_output_with_curl(
    *,
    url: str,
    token: str,
    timeout_seconds: float,
    metadata: dict[str, Any],
    file_name: str,
    content_type: str,
    output_bytes: bytes,
) -> str:
    with tempfile.TemporaryDirectory(prefix="inspection-output-upload-") as tmpdir:
        tmp_path = Path(tmpdir)
        metadata_path = tmp_path / "metadata.json"
        output_path = tmp_path / file_name
        response_body_path = tmp_path / "response-body.txt"

        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        output_path.write_bytes(output_bytes)

        command = [
            "curl",
            "-sS",
            "--show-error",
            "--request",
            "POST",
            "--max-time",
            str(timeout_seconds),
            "--output",
            str(response_body_path),
            "--write-out",
            "%{http_code}",
            "--header",
            "Accept: application/json",
            "--header",
            f"X-Telegram-Inspection-Output-Write-Token: {token}",
            "--form",
            f"metadata=@{metadata_path};type=application/json",
            "--form",
            f"file=@{output_path};filename={file_name};type={content_type}",
            url,
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception as exc:
            raise TelegramBridgeError("Failed to launch curl for inspection output upload.") from exc

        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            raise TelegramBridgeError(
                f"curl upload failed with exit code {completed.returncode}: {stderr or 'no stderr'}"
            )

        status_code = (completed.stdout or "").strip()
        response_body = response_body_path.read_text(encoding="utf-8") if response_body_path.exists() else ""

        if status_code != "200":
            print("Inspection output upload failed via curl.")
            print(f"HTTP status: {status_code or 'unknown'}")
            print(f"HTTP body: {response_body}")
            raise TelegramBridgeError(
                f"Inspection output upload failed with HTTP {status_code or 'unknown'}: {response_body}"
            )

        return response_body


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
