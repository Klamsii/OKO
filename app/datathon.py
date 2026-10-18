"""Bridge to the team's self-hosted model; no detector or label substitution."""
from __future__ import annotations

import base64
import io
import math
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from PIL import Image

from .analysis_scope import describe_analysis


class ModelError(ValueError):
    pass


def validate_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ModelError("Задайте DATATHON_API_URL: адрес собственного сервера без ключа, пароля и параметров.")
    return url.rstrip("/")


def _request(client, method, path, deadline, **kwargs):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ModelError("Истёк общий лимит времени анализа, включая очередь и передачу видео.")
    try:
        # Bound response size. Redirects are rejected: never forward the model key elsewhere.
        with client.stream(method, path, timeout=min(30, remaining), **kwargs) as response:
            if response.status_code != 200 and not (method == "POST" and response.status_code == 202):
                messages = {401: "Сервер модели отклонил ключ доступа.", 403: "Сервер модели запретил доступ.",
                            413: "Видео превышает лимит сервера модели.", 415: "Сервер модели принимает оригинальные MOV.",
                            503: "Модель ещё загружается или её очередь заполнена."}
                raise ModelError(messages.get(response.status_code, f"Сервер модели вернул HTTP {response.status_code}."))
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > 32 * 1024 * 1024 or time.monotonic() >= deadline:
                    raise ModelError("Превышен размер ответа или лимит времени сервера модели.")
            import json
            value = json.loads(content)
            if not isinstance(value, dict):
                raise ValueError()
            return value
    except httpx.HTTPError:
        raise ModelError("Нет связи с сервером модели или истёк таймаут запроса. Проверьте подключение.") from None
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, ModelError):
            raise
        raise ModelError("Сервер модели вернул некорректный JSON.") from None


def check_health(url, key):
    try:
        with httpx.Client(base_url=validate_url(url) + "/", headers={"X-API-Key": key},
                          follow_redirects=False, trust_env=False) as client:
            ready = _request(client, "GET", "health", time.monotonic() + 3).get("ready") is True
        return {"ready": ready, "message": "Qwen готов к анализу." if ready else "Qwen загружается."}
    except (ModelError, ValueError):
        return {"ready": False, "message": "Сервер Qwen недоступен или отклонил ключ. Проверьте настройки подключения."}


def _second(value, duration):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= duration + 0.1:
        raise ModelError("Модель вернула таймкод за пределами видео или некорректное число.")
    return value


def _frame(value, duration):
    timestamp = _second(value.get("timestamp_sec"), duration)
    encoded = value.get("image_base64")
    if not isinstance(encoded, str) or len(encoded) > 8 * 1024 * 1024:
        raise ModelError("Некорректный кадр-доказательство от модели.")
    try:
        raw = base64.b64decode(encoded, validate=True)
        with Image.open(io.BytesIO(raw)) as img:
            if img.format != "JPEG" or img.width * img.height > 25_000_000:
                raise ValueError()
            img.verify()
    except Exception:
        raise ModelError("Модель вернула повреждённый JPEG.") from None
    return {"timestamp_sec": timestamp, "image_base64": encoded}


DETAIL_FIELDS = ("analysis_status", "scope", "review_issues", "abstention_reasons", "stage_seconds", "elapsed_seconds",
                 "assessment_mode", "preliminary", "decision_summary", "strict_rule_verdict", "working_assumptions",
                 "possible_violations", "moment_assessments", "optional_review_failures")

TITLES = {"cash_split_unresolved": "Неясное перемещение отделённой части денег",
          "cash_at_personal_pocket": "Деньги возле личного кармана",
          "cash_left_unsecured_between_visits": "Деньги вне места хранения между клиентами",
          "employee_left_during_service": "Сотрудник покинул место во время обслуживания"}


def translate(job_id, compact, detail, video, elapsed, notes=()):
    """Keep binary API and rich report separate; never invent confidence or legal citations."""
    verdict = compact.get("verdict")
    events = compact.get("violations")
    if compact.get("status") != "done" or verdict not in ("violation", "no_violation") or not isinstance(events, list):
        raise ModelError("Сервер модели не вернул завершённый вердикт по контракту.")
    if (verdict == "violation") != bool(events):
        raise ModelError("Вердикт модели противоречит списку событий.")
    api_events, ui_events = [], []
    duration = video["duration_sec"]
    for index, event in enumerate(events):
        if not isinstance(event, dict) or not all(isinstance(event.get(k), str) and event[k].strip() for k in ("type", "description")):
            raise ModelError("Модель вернула событие без типа или описания.")
        frames = event.get("evidence_frames")
        if not isinstance(frames, list) or not 1 <= len(frames) <= 3 or any(not isinstance(f, dict) for f in frames):
            raise ModelError("Для события нужны 1–3 кадра-доказательства.")
        item = {"type": event["type"], "description": event["description"],
                "timecode_sec": _second(event.get("timecode_sec"), duration),
                "evidence_frames": [_frame(f, duration) for f in frames]}
        api_events.append(item)
        candidates = [v for v in detail.get("violations", []) if isinstance(v, dict)
                      and v.get("type") == item["type"] and v.get("key_moment_seconds") == item["timecode_sec"]]
        extra = candidates[0] if len(candidates) == 1 else {}
        bounds = {}
        for key in ("event_start_seconds", "key_moment_seconds", "event_end_seconds"):
            bounds[key] = _second(extra[key], duration) if extra.get(key) is not None else None
        start, end = bounds["event_start_seconds"], bounds["event_end_seconds"]
        if start is not None and end is not None and not start <= item["timecode_sec"] <= end:
            raise ModelError("Модель вернула противоречивые границы события.")
        basis = extra.get("normative_basis") or {}
        ui_events.append({**item, **bounds, "event_id": f"{job_id}:v:{index}", "confidence": None,
                          "source": "Qwen3-VL · сервер команды", "t_end": end,
                          "timeline_status": extra.get("timeline_status", "single_observation_only"),
                          "certainty": extra.get("certainty"), "requires_review": extra.get("requires_review", True),
                          "affects_verdict": extra.get("affects_verdict", True), "normative_basis": basis,
                          "rule": {"id": item["type"], "title": TITLES.get(item["type"].removeprefix("possible_"), item["type"]), "refs": []}})
    api = {"job_id": job_id, "status": "done", "verdict": verdict, "violations": api_events}
    report = {k: detail[k] for k in DETAIL_FIELDS if k in detail}
    for k in ("preliminary", "assessment_mode", "decision_summary", "unresolved_questions"):
        if k in compact:
            report.setdefault(k, compact[k])
    ui = {**report, "schema_version": 1, "verdict": verdict, "violations": ui_events, "review": [],
          "video": video, "activity": [], "thumbs": [], "timings": {"total_sec": elapsed},
          "coverage_sec": None, "notes": list(notes), "detectors": ["datathon"],
          "degraded": bool(notes or report.get("optional_review_failures")),
          "analysis_scope": describe_analysis(["datathon"], provenance="completed_run"),
          "model_job_id": compact["job_id"], "model_verdict": detail.get("verdict"),
          "preliminary": report.get("preliminary", True)}
    return {"api": api, "result": ui}


def analyze_remote(request, progress):
    from .oko.pipeline.video import probe
    started = time.monotonic()
    deadline = started + request["time_budget"] - 0.5  # allow atomic save before supervisor deadline
    remote = request["remote"]
    url = validate_url(remote["url"])
    path = Path(request["path"])
    progress("decode", 0.05)
    info = probe(path)
    if info.duration > request["settings"]["max_video_seconds"]:
        raise ModelError("Видео превышает допустимую длительность.")
    with httpx.Client(base_url=url + "/", headers={"X-API-Key": remote["key"]},
                      follow_redirects=False, trust_env=False) as client:
        progress("detect", 0.1)
        with path.open("rb") as stream:
            accepted = _request(client, "POST", "api/v1/analyze", deadline,
                                files={"file": ("original.mov", stream, "video/quicktime")})
        model_id = accepted.get("job_id", "")
        if not isinstance(model_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", model_id):
            raise ModelError("Сервер вернул некорректный ID задания.")
        route = f"api/v1/jobs/{model_id}"
        while True:
            result = _request(client, "GET", route, deadline)
            if result.get("job_id") != model_id:
                raise ModelError("ID ответа модели не совпал с отправленным заданием.")
            if result.get("status") == "done":
                break
            if result.get("status") == "failed":
                raise ModelError("Сервер модели завершил анализ с ошибкой. Это не означает отсутствие нарушений.")
            if result.get("status") != "processing":
                raise ModelError("Неизвестный статус задания модели.")
            # Progress is intentionally not a fabricated percentage of analyzed frames.
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        progress("evidence", 0.95)
        notes = []
        try:
            detail = _request(client, "GET", route + "/report", deadline)
            if detail.get("job_id") != model_id or detail.get("status") != "done":
                raise ModelError("Подробный отчёт не соответствует заданию.")
        except ModelError:
            detail = {}
            notes.append("Подробный отчёт модели недоступен. Сохранён основной вердикт с исходными кадрами; границы и нормы не восстановлены.")
        return translate(request["job_id"], result, detail, info.to_dict(), time.monotonic() - started, notes)
