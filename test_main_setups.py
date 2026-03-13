import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram_bridge import InspectionSetupSummary, TelegramBridgeError


telegram_module = types.ModuleType("telegram")
telegram_module.Update = object
telegram_module.BotCommand = object
sys.modules.setdefault("telegram", telegram_module)

telegram_ext_module = types.ModuleType("telegram.ext")
telegram_ext_module.ApplicationBuilder = object
telegram_ext_module.CommandHandler = object
telegram_ext_module.MessageHandler = object
telegram_ext_module.ContextTypes = SimpleNamespace(DEFAULT_TYPE=object)
telegram_ext_module.filters = SimpleNamespace()
sys.modules.setdefault("telegram.ext", telegram_ext_module)

import main


class MainSetupsTests(unittest.IsolatedAsyncioTestCase):
    async def test_cmd_setups_replies_with_active_setups(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        context = SimpleNamespace(user_data={})
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=987654321),
            message=message,
        )

        setups = [
            InspectionSetupSummary(
                setup_id="setup-1",
                setup_name="Primary Setup",
                project_id="project-9",
                selected_template_id="template-2",
                is_active=True,
            )
        ]

        with patch.object(main.asyncio, "to_thread", AsyncMock(return_value=setups)):
            await main.cmd_setups(update, context)

        message.reply_text.assert_awaited_once_with(
            "Active inspection setups:\n\n"
            "1. Primary Setup\n"
            "project_id: project-9\n"
            "selected_template_id: template-2\n"
            "\n"
            "Reply with a number to select a setup."
        )
        self.assertEqual(context.user_data["mode"], main.MODE_SETUP_SELECT)
        self.assertEqual(
            context.user_data["setup_selection_options"][0]["setup_id"], "setup-1"
        )

    async def test_cmd_setups_replies_when_no_setups_found(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        context = SimpleNamespace(user_data={})
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=987654321),
            message=message,
        )

        with patch.object(main.asyncio, "to_thread", AsyncMock(return_value=[])):
            await main.cmd_setups(update, context)

        message.reply_text.assert_awaited_once_with("No active inspection setups found.")
        self.assertEqual(context.user_data["mode"], main.MODE_NONE)

    async def test_handle_text_selects_setup_from_active_setup_list(self):
        message = SimpleNamespace(text="2", reply_text=AsyncMock())
        context = SimpleNamespace(
            user_data={
                "mode": main.MODE_SETUP_SELECT,
                "mode_started_at": main._now_utc_ts(),
                "setup_selection_options": [
                    {
                        "setup_id": "setup-1",
                        "setup_name": "Primary Setup",
                        "project_id": "project-9",
                        "selected_template_id": "template-2",
                        "is_active": True,
                    },
                    {
                        "setup_id": "setup-2",
                        "setup_name": "Secondary Setup",
                        "project_id": "project-10",
                        "selected_template_id": None,
                        "is_active": True,
                    },
                ],
            }
        )
        update = SimpleNamespace(
            message=message,
            effective_chat=SimpleNamespace(id=123),
        )

        await main.handle_text(update, context)

        message.reply_text.assert_awaited_once_with(
            "Selected setup: Secondary Setup (project-10)."
        )
        self.assertEqual(context.user_data["selected_setup"]["setup_id"], "setup-2")
        self.assertEqual(context.user_data["mode"], main.MODE_NONE)
        self.assertNotIn("setup_selection_options", context.user_data)

    async def test_cmd_currentsetup_replies_with_selected_setup(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        context = SimpleNamespace(
            user_data={
                "selected_setup": {
                    "setup_id": "setup-1",
                    "setup_name": "Primary Setup",
                    "project_id": "project-9",
                    "selected_template_id": "template-2",
                    "is_active": True,
                }
            }
        )
        update = SimpleNamespace(message=message)

        await main.cmd_currentsetup(update, context)

        message.reply_text.assert_awaited_once_with(
            "Current inspection setup:\n\n"
            "setup_name: Primary Setup\n"
            "project_id: project-9\n"
            "selected_template_id: template-2"
        )

    async def test_cmd_clearsetup_clears_selected_setup(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        context = SimpleNamespace(
            user_data={
                "mode": main.MODE_SETUP_SELECT,
                "selected_setup": {"setup_id": "setup-1"},
                "setup_selection_options": [{"setup_id": "setup-1"}],
            }
        )
        update = SimpleNamespace(message=message)

        await main.cmd_clearsetup(update, context)

        message.reply_text.assert_awaited_once_with("Cleared selected inspection setup.")
        self.assertEqual(context.user_data["mode"], main.MODE_NONE)
        self.assertNotIn("selected_setup", context.user_data)
        self.assertNotIn("setup_selection_options", context.user_data)

    async def test_cmd_setups_replies_when_user_is_unmapped(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=987654321),
            message=message,
        )

        with patch.object(
            main.asyncio,
            "to_thread",
            AsyncMock(side_effect=TelegramBridgeError("Telegram user is not mapped.")),
        ):
            await main.cmd_setups(update, SimpleNamespace())

        message.reply_text.assert_awaited_once_with(
            "Your Telegram account is not mapped to a ThinkTrace user."
        )

    async def test_cmd_setups_replies_when_bridge_fails(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=987654321),
            message=message,
        )

        with patch.object(
            main.asyncio,
            "to_thread",
            AsyncMock(side_effect=TelegramBridgeError("bridge down")),
        ):
            await main.cmd_setups(update, SimpleNamespace())

        message.reply_text.assert_awaited_once_with(
            "Unable to load inspection setups right now. Please try again later."
        )


if __name__ == "__main__":
    unittest.main()
