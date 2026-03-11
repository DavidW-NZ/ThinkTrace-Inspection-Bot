import json
import os
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

from telegram import Bot

from export_builder import build_export_text, ExportError
from template_word_builder import build_word_report_from_template

# Phase 3: AI Rewrite Layer (local direct call; graceful degrade)
from rewrite_engine import rewrite_session_if_needed, RewriteConfig


LOCAL_BACKOFF_SECONDS = [60, 300, 900]  # 1m, 5m, 15m
MAX_ATTEMPTS = 3

# Phase 3 rewrite config (A1 + C1 + Mode 1). Model defaults to gpt-4.1-mini.
REWRITE_CONFIG = RewriteConfig()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s))
    except Exception:
        return None


def get_data_root() -> Path:
    root = (os.environ.get("INSPECTION_DATA_ROOT") or "").strip()
    if not root:
        raise RuntimeError("Missing INSPECTION_DATA_ROOT environment variable.")
    return Path(root).expanduser().resolve()


@dataclass
class Paths:
    data_root: Path

    @property
    def sessions_dir(self) -> Path:
        return self.data_root / "sessions"

    @property
    def jobs_dir(self) -> Path:
        return self.data_root / "jobs"

    @property
    def pending(self) -> Path:
        return self.jobs_dir / "pending"

    @property
    def running(self) -> Path:
        return self.jobs_dir / "running"

    @property
    def done(self) -> Path:
        return self.jobs_dir / "done"

    @property
    def failed(self) -> Path:
        return self.jobs_dir / "failed"

    @property
    def outputs_dir(self) -> Path:
        return self.data_root / "outputs"


def ensure_dirs(p: Paths) -> None:
    p.sessions_dir.mkdir(parents=True, exist_ok=True)
    p.pending.mkdir(parents=True, exist_ok=True)
    p.running.mkdir(parents=True, exist_ok=True)
    p.done.mkdir(parents=True, exist_ok=True)
    p.failed.mkdir(parents=True, exist_ok=True)
    p.outputs_dir.mkdir(parents=True, exist_ok=True)


def startup_recover_running_to_pending(p: Paths) -> None:
    for job_path in sorted(p.running.glob("*.json")):
        target = p.pending / job_path.name
        try:
            job_path.replace(target)
        except Exception:
            # Best effort; leave it for manual intervention.
            continue


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_write_text(path: Path, text: str) -> None:
    """
    Atomic write: write to <file>.tmp in same directory, fsync, then os.replace().
    Prevents half-written JSON on crash/power loss.
    """
    tmp = path.with_name(path.name + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def save_json(path: Path, data: dict) -> None:
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def _recover_or_cleanup_tmp(path: Path) -> None:
    tmp = path.with_name(path.name + ".tmp")
    if not tmp.exists():
        return
    if not path.exists():
        try:
            os.replace(tmp, path)
        except Exception:
            return
    else:
        try:
            tmp.unlink()
        except Exception:
            return


def job_is_due(job: dict) -> bool:
    dt = _parse_iso(job.get("next_run_at"))
    if dt is None:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt <= _utc_now()


def pick_due_job(p: Paths) -> Path | None:
    for job_path in sorted(p.pending.glob("*.json")):
        try:
            job = load_json(job_path)
        except Exception:
            # Corrupt job: move to failed.
            try:
                job_path.replace(p.failed / job_path.name)
            except Exception:
                pass
            continue

        if job_is_due(job):
            return job_path

    return None


def acquire_job(p: Paths, job_path: Path) -> Path | None:
    target = p.running / job_path.name
    try:
        job_path.replace(target)  # atomic move within same fs
        return target
    except Exception:
        return None


def _success_marker(outputs_inspection_dir: Path) -> Path:
    return outputs_inspection_dir / "SUCCESS.json"


def mark_success(outputs_inspection_dir: Path, job: dict) -> None:
    marker = {
        "status": "SUCCESS",
        "timestamp": _utc_now_iso(),
        "export_spec": "v1.1",
        "attempt": int(job.get("attempt", 0)),
        "artifacts": ["export_v1.txt", "report.docx"],
    }
    save_json(_success_marker(outputs_inspection_dir), marker)


def notify(bot: Bot | None, chat_id: int, text: str) -> None:
    if not bot:
        return
    try:
        bot.send_message(chat_id=chat_id, text=text)
    except Exception:
        return


def schedule_retry(job: dict) -> None:
    attempt = int(job.get("attempt", 0))
    idx = max(0, min(attempt - 1, len(LOCAL_BACKOFF_SECONDS) - 1))
    backoff = LOCAL_BACKOFF_SECONDS[idx]
    job["next_run_at"] = (_utc_now() + timedelta(seconds=backoff)).isoformat()


def handle_failure(
    p: Paths,
    job_path_running: Path,
    job: dict,
    bot: Bot | None,
    err_short: str,
    err_detail: str | None,
) -> None:
    job["last_error"] = {
        "short": err_short,
        "detail": err_detail,
        "timestamp": _utc_now_iso(),
    }

    # Increment attempt
    job["attempt"] = int(job.get("attempt", 0)) + 1
    attempt = int(job["attempt"])

    chat_id = int(job.get("chat_id", 0) or 0)

    # Notify on first failure
    if attempt == 1 and not bool(job.get("error_notified_first", False)):
        notify(
            bot,
            chat_id,
            "Export failed. Retrying automatically. (Record locked.)",
        )
        job["error_notified_first"] = True

    # Decide retry vs failed
    if attempt < MAX_ATTEMPTS:
        # Backoff schedule
        backoff = LOCAL_BACKOFF_SECONDS[attempt - 1]
        job["next_run_at"] = (_utc_now() + timedelta(seconds=backoff)).isoformat()

        # Move back to pending
        target = p.pending / job_path_running.name
        save_json(job_path_running, job)
        job_path_running.replace(target)
        return

    # Final failure
    if attempt >= MAX_ATTEMPTS and not bool(job.get("error_notified_final", False)):
        notify(
            bot,
            chat_id,
            "Export failed after retries. Check local settings and rerun. (Record locked.)",
        )
        job["error_notified_final"] = True

    target = p.failed / job_path_running.name
    save_json(job_path_running, job)
    job_path_running.replace(target)


def process_one_job(p: Paths, job_path_running: Path, bot: Bot | None) -> None:
    job = load_json(job_path_running)
    inspection_id = str(job.get("inspection_id", "") or "")
    if not inspection_id:
        handle_failure(
            p,
            job_path_running,
            job,
            bot,
            "Job missing inspection_id",
            None,
        )
        return

    # Outputs dir
    out_dir = p.outputs_dir / inspection_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # Idempotency
    if _success_marker(out_dir).exists():
        job_path_running.replace(p.done / job_path_running.name)
        return

    # Load session
    session_path = p.sessions_dir / f"{inspection_id}.json"
    if not session_path.exists():
        handle_failure(
            p,
            job_path_running,
            job,
            bot,
            "Session file not found",
            str(session_path),
        )
        return

    # Recover leftover temp from atomic writes (best-effort)
    _recover_or_cleanup_tmp(session_path)

    try:
        session = load_json(session_path)
    except Exception as e:
        print("=== SESSION LOAD ERROR ===")
        traceback.print_exc()

        handle_failure(
            p,
            job_path_running,
            job,
            bot,
            "Failed loading session",
            repr(e),
        )
        return

    # Hard gate
    if str(session.get("status", "")) != "LOCKED":
        handle_failure(
            p,
            job_path_running,
            job,
            bot,
            "Session is not LOCKED",
            None,
        )
        return

    # Export text (raw)
    try:
        export_text = build_export_text(session)
    except ExportError as e:
        handle_failure(
            p,
            job_path_running,
            job,
            bot,
            f"ExportError: {e.code}",
            e.message,
        )
        return
    except Exception as e:
        handle_failure(
            p,
            job_path_running,
            job,
            bot,
            "Unexpected export error",
            repr(e),
        )
        return

    try:
        # Write raw export first (unchanged Phase 2 behaviour)
        (out_dir / "export_v1.txt").write_text(export_text, encoding="utf-8")

        # ---- Phase 3: AI Rewrite Layer (A1 + C1 + Mode 1) ----
        # Runs AFTER LOCKED snapshot, BEFORE Word generation.
        # Uses atomic checkpoint writes to session_path to survive crashes.
        def _save_session_checkpoint(updated_session: dict) -> None:
            save_json(session_path, updated_session)

        summary = rewrite_session_if_needed(
            session,
            save_checkpoint=_save_session_checkpoint,
            client=None,
            config=REWRITE_CONFIG,
        )

        # Optional: keep a lightweight run log in session (non-critical)
        session.setdefault("rewrite_run_meta", []).append(summary)
        _save_session_checkpoint(session)

        # Debug artifact (no Telegram output): helps verify AI actually ran
        save_json(out_dir / "rewrite_summary.json", summary)

        # ---- Build Word report ----
        build_word_report_from_template(session, p.data_root)

        expected_files = [
            out_dir / "export_v1.txt",
            out_dir / "report.docx",
        ]

        for f in expected_files:
            if not f.exists():
                raise RuntimeError(f"Missing artifact: {f.name}")

        mark_success(out_dir, job)
    except Exception as e:
        handle_failure(
            p,
            job_path_running,
            job,
            bot,
            "Failed writing outputs",
            repr(e),
        )
        return

    # Done
    job_path_running.replace(p.done / job_path_running.name)

    # ---- Future Extension Hook (Judgement / Risk Extraction) ----


def build_bot() -> Bot | None:
    return None
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        return None
    return Bot(token=token)


def run_forever(poll_seconds: float = 2.0) -> None:
    data_root = get_data_root()
    paths = Paths(data_root=data_root)
    ensure_dirs(paths)
    startup_recover_running_to_pending(paths)

    bot = build_bot()

    while True:
        job_path = pick_due_job(paths)
        if not job_path:
            time.sleep(poll_seconds)
            continue

        running_path = acquire_job(paths, job_path)
        if not running_path:
            continue

        try:
            process_one_job(paths, running_path, bot)
        except Exception:
            # Absolute last-resort: mark job as failed with traceback.
            try:
                job = load_json(running_path)
            except Exception:
                job = {"attempt": 0, "chat_id": 0}
            tb = traceback.format_exc(limit=10)
            handle_failure(paths, running_path, job, bot, "Worker crash", tb)


if __name__ == "__main__":
    run_forever()
