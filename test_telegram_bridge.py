import io
import json
import os
import unittest
from unittest.mock import patch

import telegram_bridge


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return io.StringIO(json.dumps(self._payload))

    def __exit__(self, exc_type, exc, tb):
        return False


class TelegramBridgeTests(unittest.TestCase):
    def test_load_config_from_env_returns_none_when_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(telegram_bridge.load_telegram_bridge_config_from_env())

    def test_fetch_inspection_setups_uses_telegram_user_id_and_bridge_token(self):
        config = telegram_bridge.TelegramBridgeConfig(
            base_url="https://example.test/root/",
            token="bridge-secret",
            timeout_seconds=3.5,
        )

        with patch.object(
            telegram_bridge,
            "urlopen",
            return_value=_FakeResponse(
                {
                    "success": True,
                    "data": {
                        "items": [
                            {
                                "setup_id": "setup-1",
                                "setup_name": "Primary Setup",
                                "project_id": "project-9",
                                "selected_template_id": "template-2",
                                "is_active": True,
                            }
                        ]
                    },
                    "error": None,
                }
            ),
        ) as mocked_urlopen:
            setups = telegram_bridge.fetch_inspection_setups(
                telegram_user_id=987654321,
                config=config,
            )

        self.assertEqual(len(setups), 1)
        self.assertEqual(setups[0].setup_id, "setup-1")
        self.assertEqual(setups[0].selected_template_id, "template-2")

        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://example.test/root/telegram/inspection-setups?telegram_user_id=987654321&active_only=true",
        )
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("X-telegram-bridge-token"), "bridge-secret")
        self.assertEqual(mocked_urlopen.call_args.kwargs["timeout"], 3.5)

    def test_fetch_inspection_setups_rejects_invalid_payload(self):
        config = telegram_bridge.TelegramBridgeConfig(
            base_url="https://example.test/",
            token="bridge-secret",
        )

        with patch.object(
            telegram_bridge,
            "urlopen",
            return_value=_FakeResponse({"unexpected": "shape"}),
        ):
            with self.assertRaises(telegram_bridge.TelegramBridgeError):
                telegram_bridge.fetch_inspection_setups(
                    telegram_user_id=123,
                    config=config,
                )

    def test_fetch_inspection_setups_rejects_unsuccessful_envelope(self):
        config = telegram_bridge.TelegramBridgeConfig(
            base_url="https://example.test/",
            token="bridge-secret",
        )

        with patch.object(
            telegram_bridge,
            "urlopen",
            return_value=_FakeResponse({"success": False, "data": {"items": []}, "error": "forbidden"}),
        ):
            with self.assertRaises(telegram_bridge.TelegramBridgeError):
                telegram_bridge.fetch_inspection_setups(
                    telegram_user_id=123,
                    config=config,
                )


if __name__ == "__main__":
    unittest.main()
