import io
import json
import os
import subprocess
import unittest
from pathlib import Path
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
            self.assertIsNone(telegram_bridge.load_inspection_output_write_config_from_env())

    def test_load_telegram_bridge_config_accepts_thinktrace_base_url_fallback(self):
        with patch.dict(
            os.environ,
            {
                "THINKTRACE_BASE_URL": "https://example.test/root",
                "TELEGRAM_BRIDGE_TOKEN": "bridge-secret",
            },
            clear=True,
        ):
            config = telegram_bridge.load_telegram_bridge_config_from_env()

        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.base_url, "https://example.test/root/")
        self.assertEqual(config.token, "bridge-secret")

    def test_load_inspection_output_write_config_reads_write_token_env(self):
        with patch.dict(
            os.environ,
            {
                "THINKTRACE_BASE_URL": "https://example.test/root",
                "TELEGRAM_INSPECTION_OUTPUT_WRITE_TOKEN": "write-secret",
            },
            clear=True,
        ):
            config = telegram_bridge.load_inspection_output_write_config_from_env()

        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.base_url, "https://example.test/root/")
        self.assertEqual(config.token, "write-secret")

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

    def test_write_inspection_output_posts_multipart_file_and_metadata(self):
        config = telegram_bridge.InspectionOutputWriteConfig(
            base_url="https://example.test/root/",
            token="write-secret",
            timeout_seconds=7.25,
        )
        captured = {}

        def _fake_run(command, capture_output, text, check):
            self.assertTrue(capture_output)
            self.assertTrue(text)
            self.assertFalse(check)
            captured["command"] = command
            response_path = Path(command[command.index("--output") + 1])
            metadata_arg = command[command.index("--form") + 1]
            file_arg = command[command.index("--form", command.index("--form") + 1) + 1]
            metadata_path = Path(metadata_arg.split("@", 1)[1].split(";", 1)[0])
            file_path = Path(file_arg.split("@", 1)[1].split(";", 1)[0])
            captured["metadata_text"] = metadata_path.read_text(encoding="utf-8")
            captured["file_bytes"] = file_path.read_bytes()
            response_path.write_text(
                json.dumps({"success": True, "data": {"stored": True}, "error": None}),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0, stdout="200", stderr="")

        with patch.object(telegram_bridge.subprocess, "run", side_effect=_fake_run):
            telegram_bridge.write_inspection_output(
                file_name="report.docx",
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                output_bytes=b"docx-bytes",
                metadata={
                    "telegram_user_id": 987654321,
                    "inspection_id": "inspection-1",
                    "project_id": "project-9",
                    "output_type": "report",
                },
                config=config,
            )

        command = captured["command"]
        self.assertEqual(command[0], "curl")
        self.assertIn("https://example.test/root/telegram/inspection-outputs", command)
        self.assertIn("7.25", command)
        self.assertIn("Accept: application/json", command)
        self.assertIn("X-Telegram-Inspection-Output-Write-Token: write-secret", command)

        metadata_arg = command[command.index("--form") + 1]
        file_arg = command[command.index("--form", command.index("--form") + 1) + 1]
        self.assertIn("metadata=@", metadata_arg)
        self.assertIn(";type=application/json", metadata_arg)
        self.assertIn("file=@", file_arg)
        self.assertIn("filename=report.docx", file_arg)
        self.assertIn(
            "type=application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            file_arg,
        )
        self.assertIn('"telegram_user_id": 987654321', captured["metadata_text"])
        self.assertEqual(captured["file_bytes"], b"docx-bytes")

    def test_write_inspection_output_uses_env_write_token_header(self):
        with patch.dict(
            os.environ,
            {
                "THINKTRACE_BASE_URL": "https://example.test/root",
                "TELEGRAM_INSPECTION_OUTPUT_WRITE_TOKEN": "write-secret",
            },
            clear=True,
        ):
            captured = {}

            def _fake_run(command, capture_output, text, check):
                captured["command"] = command
                response_path = Path(command[command.index("--output") + 1])
                response_path.write_text(
                    json.dumps({"success": True, "data": {"stored": True}, "error": None}),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 0, stdout="200", stderr="")

            with patch.object(telegram_bridge.subprocess, "run", side_effect=_fake_run):
                telegram_bridge.write_inspection_output(
                    file_name="report.docx",
                    content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    output_bytes=b"docx-bytes",
                    metadata={
                        "telegram_user_id": 987654321,
                        "inspection_id": "inspection-1",
                        "project_id": "project-9",
                        "output_type": "report",
                    },
                )

        command = captured["command"]
        self.assertIn("X-Telegram-Inspection-Output-Write-Token: write-secret", command)

    def test_write_inspection_output_does_not_send_without_write_token(self):
        config = telegram_bridge.InspectionOutputWriteConfig(
            base_url="https://example.test/root/",
            token="   ",
        )

        with patch.object(telegram_bridge.subprocess, "run") as mocked_run:
            with self.assertRaises(telegram_bridge.TelegramBridgeError):
                telegram_bridge.write_inspection_output(
                    file_name="report.docx",
                    content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    output_bytes=b"docx-bytes",
                    metadata={
                        "telegram_user_id": 987654321,
                        "inspection_id": "inspection-1",
                        "project_id": "project-9",
                        "output_type": "report",
                    },
                    config=config,
                )

        mocked_run.assert_not_called()

    def test_write_inspection_output_raises_on_non_200_response(self):
        config = telegram_bridge.InspectionOutputWriteConfig(
            base_url="https://example.test/root/",
            token="write-secret",
        )

        def _fake_run(command, capture_output, text, check):
            response_path = Path(command[command.index("--output") + 1])
            response_path.write_text('{"success":false,"error":"forbidden"}', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="403", stderr="")

        with patch.object(telegram_bridge.subprocess, "run", side_effect=_fake_run):
            with self.assertRaises(telegram_bridge.TelegramBridgeError) as ctx:
                telegram_bridge.write_inspection_output(
                    file_name="report.docx",
                    content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    output_bytes=b"docx-bytes",
                    metadata={
                        "telegram_user_id": 987654321,
                        "inspection_id": "inspection-1",
                        "project_id": "project-9",
                        "output_type": "report",
                    },
                    config=config,
                )

        self.assertIn("HTTP 403", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
