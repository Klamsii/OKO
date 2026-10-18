import logging
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from .config import Settings
from .db import claim_next, fail, finish, init, purge_expired, recover_interrupted
from .media import jpeg_frame, probe_video
from .model import load_rules, run_model
from . import db


log = logging.getLogger(__name__)


def process_job(job: dict, settings: Settings, rules: dict) -> None:
    if job.get("origin") in ("ui", "api"):
        return process_integrated_job(job, settings)
    path = Path(job["file_path"])
    deadline = time.monotonic() + 280
    try:
        duration = probe_video(path, settings, deadline)
        events = run_model(path, duration, settings, rules, deadline)
        violations = []
        for event in events:
            definition = rules[event["event_type"]]
            violations.append({
                "type": definition["label"],
                "timecode_sec": event["timestamp_sec"],
                "description": f"{event['observation']} Возможный признак для проверки: {definition['reason']}.",
                "evidence_frames": [
                    {"timestamp_sec": second, "image_base64": jpeg_frame(path, second, settings, deadline)}
                    for second in event["frame_times"]
                ],
                "rule": definition["rule"],
            })
        result = {"job_id": job["id"], "status": "done",
                  "verdict": "violation" if violations else "no_violation",
                  "violations": violations}
        finish(settings.data_dir, job["id"], result)
    except Exception as exc:
        log.exception("Обработка задания %s завершилась ошибкой", job["id"])
        fail(settings.data_dir, job["id"], str(exc) or type(exc).__name__)
    finally:
        path.unlink(missing_ok=True)


def _stop_process(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=5)
        except subprocess.SubprocessError:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=5)


def process_integrated_job(job: dict, settings: Settings, stop_event=None) -> None:
    snapshot = json.loads(job.get("config_json") or "{}")
    settings = replace(settings, **{key: snapshot[key] for key in (
        "threshold", "review_threshold", "sample_fps", "deadline_seconds", "max_video_seconds", "analysis_backend") if key in snapshot},
        enable_team_model="team" in snapshot.get("detectors", settings.pipeline_settings().detectors))
    token = job.get("execution_token")
    output = settings.data_dir / "staged" / f"{job['id']}.json"
    progress_file = settings.data_dir / "staged" / f"{job['id']}.progress.json"
    log_file = settings.data_dir / "staged" / f"{job['id']}.error.txt"
    output.parent.mkdir(parents=True, exist_ok=True)
    accepted = job.get("accepted_at") or job.get("created_epoch")
    if not accepted:
        try:
            accepted = datetime.fromisoformat(job["created_at"].replace("Z", "+00:00")).replace(tzinfo=timezone.utc).timestamp()
        except (ValueError, KeyError, TypeError):
            accepted = time.time()
    budget = settings.deadline_seconds - max(0, time.time() - float(accepted))
    process = None
    try:
        if budget <= 0:
            raise ValueError("Истёк лимит времени ожидания анализа")
        options = asdict(settings.pipeline_settings())
        options["data_dir"] = str(settings.data_dir)
        request = {"job_id": job["id"], "path": job["file_path"], "settings": options,
                   "time_budget": budget, "output_path": str(output), "progress_path": str(progress_file)}
        if settings.analysis_backend == "datathon":
            # Credentials travel only over the child's stdin, never command-line, DB or logs.
            request["remote"] = {"url": settings.datathon_api_url, "key": settings.datathon_api_key}
        environment = {key: value for key, value in os.environ.items()
                       if not any(secret in key.upper() for secret in ("API_KEY", "TOKEN", "COOKIE", "PASSWORD", "SECRET"))}
        environment.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        deadline = time.monotonic() + budget
        # The reader lease also prevents deletion of a shared video while a model reads it.
        with db.asset_reader(settings.data_dir, job["id"]) as _asset:
            with log_file.open("wb") as errors:
                process = subprocess.Popen(
                    [sys.executable, "-m", "app.analysis_process"], stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL, stderr=errors, env=environment,
                    cwd=str(Path(__file__).resolve().parent.parent),
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                    start_new_session=os.name != "nt",
                )
                process.stdin.write(json.dumps(request).encode("utf-8"))
                process.stdin.close()
                last_progress = None
                while process.poll() is None:
                    current = db.get_job(settings.data_dir, job["id"])
                    if current is None or current.get("deleted_at") or (stop_event and stop_event.is_set()):
                        _stop_process(process)
                        raise ValueError("Обработка прервана")
                    if time.monotonic() >= deadline:
                        _stop_process(process)
                        raise ValueError("Анализ не уложился в лимит времени")
                    try:
                        progress = progress_file.read_text(encoding="utf-8")
                        if progress != last_progress:
                            data = json.loads(progress)
                            db.set_progress(settings.data_dir, job["id"], data["stage"], data["progress"], token)
                            last_progress = progress
                    except (OSError, ValueError, KeyError):
                        pass
                    time.sleep(0.15)
            if process.returncode:
                error = log_file.read_text(encoding="utf-8", errors="replace").strip().splitlines()
                raise ValueError(error[-1][:500] if error else "Ошибка локального анализа")
            payload = json.loads(output.read_text(encoding="utf-8"))
            if settings.analysis_backend == "datathon":
                timings = payload["result"].setdefault("timings", {})
                timings["worker_sec"] = timings.get("total_sec")
                timings["total_sec"] = max(0, time.time() - float(accepted))
                timings["includes_queue"] = True
                # Keep save-retry data identical to the report accepted into SQLite.
                from .analysis_process import atomic_json
                atomic_json(output, payload)
            if db.complete_run(settings.data_dir, job["id"], payload["api"], payload["result"], token):
                output.unlink(missing_ok=True)
    except Exception as exc:
        if process is not None and process.poll() is None:
            _stop_process(process)
        if output.exists():
            # A completed staged report remains available for save retry, without model rerun.
            db.mark_save_failed(settings.data_dir, job["id"])
            log.warning("Готовый отчёт %s ожидает повторного сохранения", job["id"])
        else:
            db.fail(settings.data_dir, job["id"], str(exc)[:500], execution_token=token)
    finally:
        progress_file.unlink(missing_ok=True)
        log_file.unlink(missing_ok=True)
        db.release_worker(settings.data_dir, job["id"], token)


class WorkerService:
    """One persistent queue; atomic SQLite claims allow separate deployment workers."""
    def __init__(self, settings):
        self.settings = settings
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="forte-queue", daemon=True)

    def start(self):
        db.recover_interrupted(self.settings.data_dir)
        self.thread.start()
        return self

    def _loop(self):
        active = set()
        with ThreadPoolExecutor(max_workers=self.settings.worker_concurrency) as pool:
            while not self.stop.is_set():
                active = {task for task in active if not task.done()}
                while len(active) < self.settings.worker_concurrency and not self.stop.is_set():
                    job = db.claim_next(self.settings.data_dir)
                    if not job:
                        break
                    if job.get("origin") in ("ui", "api"):
                        active.add(pool.submit(process_integrated_job, job, self.settings, self.stop))
                    else:
                        active.add(pool.submit(process_job, job, self.settings, load_rules(self.settings.rules_path)))
                self.stop.wait(0.25)

    def shutdown(self):
        self.stop.set()
        self.thread.join(timeout=8)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    init(settings.data_dir)
    service = WorkerService(settings).start()
    log.info("Локальный обработчик запущен")
    try:
        while not service.stop.wait(60):
            purge_expired(settings.data_dir, settings.retention_hours)
    except KeyboardInterrupt:
        service.shutdown()


if __name__ == "__main__":
    main()
