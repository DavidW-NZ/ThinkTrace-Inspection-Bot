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
    async def test_cmd_start_starts_session_directly_from_selected_setup(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        context = SimpleNamespace(
            user_data={
                "selected_setup": {
                    "setup_id": "setup-1",
                    "setup_name": "Primary Setup",
                    "project_id": "project-9",
                    "selected_template_id": "template-2",
                    "is_active": True,
                },
                "projects_cache": ["project-old"],
                "mode": main.MODE_PROJECT_SELECT,
                "mode_started_at": main._now_utc_ts(),
            }
        )
        update = SimpleNamespace(
            message=message,
            effective_chat=SimpleNamespace(id=123),
        )

        with (
            patch.object(main, "generate_inspection_id", return_value="project-9-20260313-120000"),
            patch.object(main, "_utc_now_iso", return_value="2026-03-13T12:00:00Z"),
            patch.object(main.session_store, "set_active_inspection_id") as mock_set_active,
            patch.object(main.session_store, "save_session") as mock_save_session,
            patch.object(main, "load_projects") as mock_load_projects,
        ):
            await main.cmd_start(update, context)

        saved_session = mock_save_session.call_args.args[0]
        self.assertEqual(saved_session["project_id"], "project-9")
        self.assertEqual(
            saved_session["selected_setup"],
            {
                "setup_id": "setup-1",
                "setup_name": "Primary Setup",
                "project_id": "project-9",
                "selected_template_id": "template-2",
            },
        )
        mock_set_active.assert_called_once_with(123, "project-9-20260313-120000")
        mock_load_projects.assert_not_called()
        self.assertEqual(context.user_data["mode"], main.MODE_NONE)
        self.assertNotIn("projects_cache", context.user_data)
        message.reply_text.assert_awaited_once_with(
            "Session started from selected setup.\n"
            "Project: project-9\n"
            "Inspection: project-9-20260313-120000\n"
            "CAPTURING.\n"
            "Applied setup: Primary Setup (setup-1)."
        )

    async def test_cmd_start_without_selected_setup_keeps_project_selection_flow(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        context = SimpleNamespace(user_data={})
        update = SimpleNamespace(message=message)

        with patch.object(main, "load_projects", return_value=["project-9", "project-10"]):
            await main.cmd_start(update, context)

        self.assertEqual(context.user_data["projects_cache"], ["project-9", "project-10"])
        self.assertEqual(context.user_data["mode"], main.MODE_PROJECT_SELECT)
        message.reply_text.assert_awaited_once_with(
            "Select a project by number:\n1. project-9\n2. project-10"
        )

    async def test_handle_text_project_select_injects_selected_setup_into_new_session(self):
        message = SimpleNamespace(text="1", reply_text=AsyncMock())
        context = SimpleNamespace(
            user_data={
                "mode": main.MODE_PROJECT_SELECT,
                "mode_started_at": main._now_utc_ts(),
                "projects_cache": ["project-9"],
                "selected_setup": {
                    "setup_id": "setup-1",
                    "setup_name": "Primary Setup",
                    "project_id": "project-9",
                    "selected_template_id": "template-2",
                    "is_active": True,
                },
            }
        )
        update = SimpleNamespace(
            message=message,
            effective_chat=SimpleNamespace(id=123),
        )

        with (
            patch.object(main, "generate_inspection_id", return_value="project-9-20260313-120000"),
            patch.object(main, "_utc_now_iso", return_value="2026-03-13T12:00:00Z"),
            patch.object(main.session_store, "set_active_inspection_id") as mock_set_active,
            patch.object(main.session_store, "save_session") as mock_save_session,
        ):
            await main.handle_text(update, context)

        saved_session = mock_save_session.call_args.args[0]
        self.assertEqual(
            saved_session["selected_setup"],
            {
                "setup_id": "setup-1",
                "setup_name": "Primary Setup",
                "project_id": "project-9",
                "selected_template_id": "template-2",
            },
        )
        self.assertNotIn("is_active", saved_session["selected_setup"])
        mock_set_active.assert_called_once_with(123, "project-9-20260313-120000")
        message.reply_text.assert_awaited_once_with(
            "Session started.\n"
            "Project: project-9\n"
            "Inspection: project-9-20260313-120000\n"
            "CAPTURING.\n"
            "Applied setup: Primary Setup (setup-1)."
        )

    async def test_handle_text_project_select_leaves_session_unchanged_without_selected_setup(self):
        message = SimpleNamespace(text="1", reply_text=AsyncMock())
        context = SimpleNamespace(
            user_data={
                "mode": main.MODE_PROJECT_SELECT,
                "mode_started_at": main._now_utc_ts(),
                "projects_cache": ["project-9"],
            }
        )
        update = SimpleNamespace(
            message=message,
            effective_chat=SimpleNamespace(id=123),
        )

        with (
            patch.object(main, "generate_inspection_id", return_value="project-9-20260313-120000"),
            patch.object(main, "_utc_now_iso", return_value="2026-03-13T12:00:00Z"),
            patch.object(main.session_store, "set_active_inspection_id"),
            patch.object(main.session_store, "save_session") as mock_save_session,
        ):
            await main.handle_text(update, context)

        saved_session = mock_save_session.call_args.args[0]
        self.assertNotIn("selected_setup", saved_session)
        message.reply_text.assert_awaited_once_with(
            "Session started.\n"
            "Project: project-9\n"
            "Inspection: project-9-20260313-120000\n"
            "CAPTURING."
        )

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
