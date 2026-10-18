"""One killable, network-free-by-design local video-analysis invocation."""
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

from .oko.config import Settings
from .oko.pipeline.analyzer import analyze
from .analysis_scope import describe_analysis


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        for attempt in range(12):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 11:
                    raise
                time.sleep(0.02)
    finally:
        temporary.unlink(missing_ok=True)


def emit_progress(path, stage, progress):
    try:
        atomic_json(path, {"stage": stage, "progress": progress})
    except OSError:
        # Progress is advisory. Windows readers may briefly hold the destination;
        # that must never invalidate an otherwise successful video analysis.
        pass


def main():
    request = json.load(sys.stdin)
    if "remote" in request:
        from .datathon import analyze_remote
        payload = analyze_remote(request, lambda stage, progress: emit_progress(request["progress_path"], stage, progress))
        atomic_json(request["output_path"], payload)
        return
    options = request["settings"]
    options["data_dir"] = Path(options["data_dir"])
    options["detectors"] = tuple(options["detectors"])
    options["team_model_command"] = tuple(options.get("team_model_command", ()))
    settings = Settings(**options)
    deadline = time.monotonic() + max(0.1, request["time_budget"])
    progress_path = Path(request["progress_path"])
    result = analyze(Path(request["path"]), settings, deadline,
                     progress=lambda stage, progress: emit_progress(progress_path, stage, progress))
    findings = [f.to_ui() for f in result.findings]
    review = [f.to_ui() for f in result.review]
    for kind, events in (("v", findings), ("r", review)):
        for index, event in enumerate(events):
            event["event_id"] = f"{request['job_id']}:{kind}:{index}"
    api = {"job_id": request["job_id"], "status": "done", "verdict": result.verdict,
           "violations": [f.to_api() for f in result.findings]}
    ui = {"schema_version": 1, "verdict": result.verdict, "violations": findings,
          "review": review, "video": result.video, "activity": result.activity,
          "thumbs": result.thumbs, "timings": result.timings, "coverage_sec": result.coverage_sec,
          "degraded": result.degraded, "notes": result.notes, "detectors": result.detectors,
          "threshold": settings.threshold}
    ui["analysis_scope"] = describe_analysis(result.detectors,
        command_configured=bool(settings.team_model_command), provenance="completed_run")
    atomic_json(request["output_path"], {"api": api, "result": ui})


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # No request, environment, video bytes or model output is logged here.
        message = "Не удалось прочитать или сохранить данные анализа." if isinstance(exc, OSError) else str(exc)[:500] or type(exc).__name__
        print(message, file=sys.stderr)
        raise SystemExit(1)
