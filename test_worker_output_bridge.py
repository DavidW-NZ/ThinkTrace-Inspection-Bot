import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


telegram_module = types.ModuleType("telegram")
telegram_module.Bot = object
sys.modules.setdefault("telegram", telegram_module)

export_builder_module = types.ModuleType("export_builder")


class ExportError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


export_builder_module.ExportError = ExportError
export_builder_module.build_export_text = lambda session: "export-text"
sys.modules.setdefault("export_builder", export_builder_module)

template_word_builder_module = types.ModuleType("template_word_builder")
template_word_builder_module.build_word_report_from_template = lambda session, data_root: None
sys.modules.setdefault("template_word_builder", template_word_builder_module)

telegram_bridge_module = types.ModuleType("telegram_bridge")
telegram_bridge_module.write_inspection_output = lambda **kwargs: None
sys.modules.setdefault("telegram_bridge", telegram_bridge_module)

rewrite_engine_module = types.ModuleType("rewrite_engine")


class RewriteConfig:
    pass


rewrite_engine_module.RewriteConfig = RewriteConfig
rewrite_engine_module.rewrite_session_if_needed = (
    lambda session, save_checkpoint, client, config: {"status": "skipped"}
)
sys.modules.setdefault("rewrite_engine", rewrite_engine_module)

worker = importlib.import_module("worker")


class WorkerOutputBridgeTests(unittest.TestCase):
    def test_process_one_job_hands_off_report_when_artifacts_exist(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_root = Path(tmpdir)
            paths, running_job_path, inspection_id = self._prepare_job_files(
                temp_root,
                telegram_user_id=987654321,
            )

            with (
                patch.object(worker, "build_export_text", return_value="export-body"),
                patch.object(worker, "rewrite_session_if_needed", return_value={"status": "ok"}),
                patch.object(
                    worker,
                    "build_word_report_from_template",
                    side_effect=self._build_report_artifact(temp_root, inspection_id),
                ),
                patch.object(worker, "write_inspection_output") as mock_write_output,
            ):
                worker.process_one_job(paths, running_job_path, bot=None)

            mock_write_output.assert_called_once()
            self.assertTrue((paths.done / running_job_path.name).exists())

    def test_process_one_job_handoff_metadata_includes_telegram_user_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_root = Path(tmpdir)
            paths, running_job_path, inspection_id = self._prepare_job_files(
                temp_root,
                telegram_user_id=987654321,
            )

            with (
                patch.object(worker, "build_export_text", return_value="export-body"),
                patch.object(worker, "rewrite_session_if_needed", return_value={"status": "ok"}),
                patch.object(
                    worker,
                    "build_word_report_from_template",
                    side_effect=self._build_report_artifact(temp_root, inspection_id),
                ),
                patch.object(worker, "write_inspection_output") as mock_write_output,
            ):
                worker.process_one_job(paths, running_job_path, bot=None)

            metadata = mock_write_output.call_args.kwargs["metadata"]
            self.assertEqual(metadata["telegram_user_id"], 987654321)
            self.assertEqual(
                metadata,
                {
                    "telegram_user_id": 987654321,
                    "inspection_id": inspection_id,
                    "project_id": "project-9",
                    "output_type": "report",
                },
            )

    def test_process_one_job_does_not_handoff_when_report_artifact_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_root = Path(tmpdir)
            paths, running_job_path, _ = self._prepare_job_files(
                temp_root,
                telegram_user_id=987654321,
            )

            with (
                patch.object(worker, "build_export_text", return_value="export-body"),
                patch.object(worker, "rewrite_session_if_needed", return_value={"status": "ok"}),
                patch.object(worker, "build_word_report_from_template"),
                patch.object(worker, "write_inspection_output") as mock_write_output,
            ):
                worker.process_one_job(paths, running_job_path, bot=None)

            mock_write_output.assert_not_called()
            pending_job = worker.load_json(paths.pending / running_job_path.name)
            self.assertEqual(pending_job["last_error"]["short"], "Failed writing outputs")
            self.assertIn("Missing artifact: report.docx", pending_job["last_error"]["detail"])

    def test_process_one_job_does_not_handoff_when_telegram_user_id_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_root = Path(tmpdir)
            paths, running_job_path, _ = self._prepare_job_files(
                temp_root,
                telegram_user_id=None,
            )

            with patch.object(worker, "write_inspection_output") as mock_write_output:
                worker.process_one_job(paths, running_job_path, bot=None)

            mock_write_output.assert_not_called()
            pending_job = worker.load_json(paths.pending / running_job_path.name)
            self.assertEqual(pending_job["last_error"]["short"], "Job missing telegram_user_id")

    def _prepare_job_files(
        self,
        temp_root: Path,
        *,
        telegram_user_id: int | None,
    ) -> tuple[worker.Paths, Path, str]:
        paths = worker.Paths(data_root=temp_root)
        worker.ensure_dirs(paths)

        inspection_id = "inspection-123"
        session = {
            "inspection_id": inspection_id,
            "project_id": "project-9",
            "status": "LOCKED",
            "selected_setup": {
                "setup_id": "setup-1",
                "setup_name": "Primary Setup",
                "selected_template_id": "template-2",
            },
        }
        worker.save_json(paths.sessions_dir / f"{inspection_id}.json", session)

        job = {
            "inspection_id": inspection_id,
            "chat_id": 123,
        }
        if telegram_user_id is not None:
            job["telegram_user_id"] = telegram_user_id

        running_job_path = paths.running / "job-1.json"
        worker.save_json(running_job_path, job)
        return paths, running_job_path, inspection_id

    def _build_report_artifact(self, temp_root: Path, inspection_id: str):
        def _side_effect(session: dict, data_root: Path) -> None:
            self.assertEqual(data_root, temp_root)
            report_path = data_root / "outputs" / inspection_id / "report.docx"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_bytes(b"report-bytes")

        return _side_effect


if __name__ == "__main__":
    unittest.main()
