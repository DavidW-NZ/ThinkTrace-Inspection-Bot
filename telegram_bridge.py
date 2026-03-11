import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen


DEFAULT_TIMEOUT_SECONDS = 10.0


class TelegramBridgeError(RuntimeError):
    pass


class TelegramUserNotMappedError(TelegramBridgeError):
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


def load_telegram_bridge_config_from_env() -> TelegramBridgeConfig | None:
    base_url = (os.environ.get("THINKTRACE_BRIDGE_BASE_URL") or "").strip()
    token = (os.environ.get("TELEGRAM_BRIDGE_TOKEN") or "").strip()

    if not base_url and not token:
        return None
    if not base_url or not token:
        raise TelegramBridgeError(
            "Both THINKTRACE_BRIDGE_BASE_URL and TELEGRAM_BRIDGE_TOKEN are required."
        )

    return TelegramBridgeConfig(base_url=base_url.rstrip("/") + "/", token=token)


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
            "Telegram bridge is not configured. Missing THINKTRACE_BRIDGE_BASE_URL and TELEGRAM_BRIDGE_TOKEN."
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
    except TelegramBridgeError:
        raise
    except Exception as exc:
        raise TelegramBridgeError("Failed to fetch inspection setups from ThinkTrace bridge.") from exc

    if not isinstance(payload, dict):
        raise TelegramBridgeError("ThinkTrace bridge returned an invalid inspection setups payload.")

    if payload.get("success") is not True:
        _raise_unsuccessful_response(payload)

    data = payload.get("data")
    if not isinstance(data, dict):
        raise TelegramBridgeError("ThinkTrace bridge returned an invalid inspection setups data envelope.")

    items = data.get("items")
    if not isinstance(items, list):
        raise TelegramBridgeError("ThinkTrace bridge returned an invalid inspection setups items list.")

    return [_parse_inspection_setup(item) for item in items]


def _raise_unsuccessful_response(payload: dict[str, Any]) -> None:
    error = payload.get("error")
    code, message = _extract_error_details(error)
    normalized = " ".join(x for x in (str(code or "").strip(), str(message or "").strip()) if x).lower()

    if any(token in normalized for token in ("unmapped", "not mapped", "mapping_not_found", "telegram_user_not_mapped")):
        raise TelegramUserNotMappedError(
            message or "Your Telegram account is not mapped to a ThinkTrace user."
        )

    raise TelegramBridgeError(
        message or "ThinkTrace bridge reported an unsuccessful inspection setups response."
    )


def _extract_error_details(error: Any) -> tuple[str | None, str | None]:
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message")
        return (
            None if code is None else str(code),
            None if message is None else str(message),
        )

    if error is None:
        return None, None

    return None, str(error)


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
