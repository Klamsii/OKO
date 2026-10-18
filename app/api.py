"""oko API: strict jury contract and private persistent UI."""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from starlette.concurrency import run_in_threadpool

from .config import Settings
from .analysis_scope import describe_analysis, scope_for_run
from .static_assets import FrontendStaticFiles
from . import db
from .oko.rules.catalog import catalog_payload

settings = Settings.from_env()
web_dir = Path(__file__).resolve().parent.parent / "web"
ALLOWED_EXT = (".mov", ".qt", ".mp4", ".m4v", ".avi", ".mkv", ".webm")
VIDEO_MIME = {".mov": "video/quicktime", ".qt": "video/quicktime", ".mp4": "video/mp4",
              ".m4v": "video/mp4", ".avi": "video/x-msvideo", ".mkv": "video/x-matroska", ".webm": "video/webm"}
STAGES = {"queued": "В очереди", "decode": "Чтение видео", "detect": "Автоматическая проверка",
          "evidence": "Кадры-доказательства", "done": "Готово", "failed": "Ошибка", "interrupted": "Прервано"}
COOKIE = "forte_session"


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init(settings.data_dir)
    for name in ("uploads", "staged"):
        (settings.data_dir / name).mkdir(parents=True, exist_ok=True)
    service = None
    if settings.embedded_worker:
        from .worker import WorkerService
        service = WorkerService(settings).start()
    yield
    if service:
        service.shutdown()


app = FastAPI(title="OKO", version="1.1.0", lifespan=lifespan)
app.mount("/static", FrontendStaticFiles(directory=web_dir), name="static")
# A release-specific directory keeps relative JS imports and CSS URLs in the
# same release too. Querying only app.js does not refresh its cached imports.
app.mount("/_ui/{revision}", FrontendStaticFiles(directory=web_dir), name="frontend")
local_pack_dir = web_dir.parent / "private_asset_pack" / "minecraft" / "1.21.10"
local_game_assets = os.getenv("ENABLE_LOCAL_GAME_ASSETS") == "1" and local_pack_dir.is_dir()
if local_game_assets:
    app.mount("/local-pack", FrontendStaticFiles(directory=local_pack_dir), name="local-pack")


def key_valid(value):
    return bool(value) and hmac.compare_digest(str(value).encode("utf-8"), settings.api_key.encode("utf-8"))


def require_key(x_api_key: str | None = Header(default=None)):
    if not key_valid(x_api_key):
        raise HTTPException(401, "Неверный X-API-Key")


def _browser_origin(request):
    # A TLS-terminating proxy can forward HTTPS browser traffic to this app
    # over HTTP. Pin the browser-facing origin in that deployment instead of
    # trusting a client-supplied X-Forwarded-Proto header.
    return os.getenv("OKO_PUBLIC_ORIGIN", "").strip().rstrip("/") or f"{request.url.scheme}://{request.headers.get('host', '')}"


def _same_origin(request):
    origin = request.headers.get("origin")
    if origin:
        parsed = urlsplit(origin)
        expected = urlsplit(_browser_origin(request))
        if (parsed.scheme != expected.scheme or parsed.netloc.lower() != expected.netloc.lower()
                or parsed.path or parsed.query or parsed.fragment):
            raise HTTPException(403, "Запрос из другого источника запрещён")


def _session_token():
    payload = f"{int(time.time())}:{secrets.token_urlsafe(18)}"
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    signature = hmac.new(settings.api_key.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return encoded + "." + signature


def _set_session_cookie(response: Response, request: Request):
    response.set_cookie(COOKIE, _session_token(), max_age=settings.session_hours * 3600,
                        httponly=True, samesite="strict", secure=_browser_origin(request).startswith("https://"), path="/")
    response.headers["Cache-Control"] = "no-store"


def _has_session(request):
    raw = request.cookies.get(COOKIE, "")
    try:
        encoded, signature = raw.split(".", 1)
        expected = hmac.new(settings.api_key.encode(), encoded.encode(), hashlib.sha256).hexdigest()
        timestamp = int(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode().split(":")[0])
        age = time.time() - timestamp
        return 0 <= age <= settings.session_hours * 3600 and hmac.compare_digest(expected, signature)
    except (ValueError, UnicodeError):
        return False


def require_ui(request: Request):
    if not (_has_session(request) or key_valid(request.headers.get("X-API-Key"))):
        raise HTTPException(401, "Войдите с ключом доступа")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        _same_origin(request)


def capabilities_body():
    detectors = list(settings.active_detectors())
    return {"version": "1.1.0", "formats": list(settings.formats()),
            "max_upload_bytes": settings.max_upload_bytes, "max_video_seconds": settings.max_video_seconds,
            "deadline_sec": settings.deadline_seconds, "sample_fps": settings.sample_fps,
            "detectors": detectors,
            "analysis_scope": describe_analysis(detectors, command_configured=bool(settings.model_command)),
            "local_only": True, "analysis_backend": settings.analysis_backend, "archive_scope": "installation",
            "local_asset_pack": "/local-pack/manifest.json" if local_game_assets else None}


def _not_found():
    return HTTPException(404, "Проверка не найдена")


def _public_dates(run):
    run["analysis_scope"] = scope_for_run(run)
    # SQLite CURRENT_TIMESTAMP is UTC; make that explicit for browser parsing.
    for field in ("created_at", "updated_at", "completed_at"):
        value = run.get(field)
        if isinstance(value, str) and len(value) == 19 and value[10] == " ":
            run[field] = value.replace(" ", "T") + "Z"
    asset = run.get("asset")
    if asset:
        run["duration_sec"] = asset.get("duration_sec")
    return run


def _run(job_id):
    run = db.get_run(settings.data_dir, job_id)
    if run is None:
        raise _not_found()
    stage_file = settings.data_dir / "staged" / f"{job_id}.json"
    if run["status"] != "done" and stage_file.is_file():
        try:
            staged = json.loads(stage_file.read_text(encoding="utf-8"))
            run.update(result=staged["result"], api_result=staged["api"], status="done",
                       archive_status="save_failed", stage="done", progress=1, error=None)
        except (OSError, KeyError, ValueError):
            pass
    run.update(stage_label=STAGES.get(run.get("stage"), run.get("stage", "")),
               deadline_sec=settings.deadline_seconds, queue_position=0)
    if run.get("stage") == "detect":
        run["stage_label"] = scope_for_run(run)["label"]
    if run.get("status") == "queued":
        with db.connect(settings.data_dir) as conn:
            run["queue_position"] = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status='queued' AND deleted_at IS NULL AND (created_at < ? OR (created_at=? AND rowid <= (SELECT rowid FROM jobs WHERE id=?)))",
                (run["created_at"], run["created_at"], job_id)).fetchone()[0]
    if run.get("asset"):
        run["asset"]["available"] = run["asset"]["availability"] == "available"
    return _public_dates(run)


def _jury(run):
    body = run.get("api_result")
    if body:
        # Remove legacy FORTE extras while preserving the established jury schema.
        result = {"job_id": run["job_id"], "status": body.get("status", "done"),
                "verdict": body.get("verdict"), "violations": [
                    {key: event[key] for key in ("type", "timecode_sec", "description", "evidence_frames") if key in event}
                    for event in body.get("violations", [])]}
        if result["status"] == "failed":
            result["error"] = body.get("error") or run.get("error") or "Ошибка анализа"
        return result
    status = "processing" if run["status"] in ("queued", "processing") else run["status"]
    result = {"job_id": run["job_id"], "status": status, "verdict": None, "violations": []}
    if status == "failed":
        result["error"] = run.get("error") or "Ошибка анализа"
    return result


def _frontend_revision():
    """Change resource URLs whenever the local frontend changes."""
    digest = hashlib.sha256()
    for path in sorted(web_dir.rglob("*")):
        if path.is_file():
            try:
                info = path.stat()
            except OSError:
                continue
            digest.update(f"{path.relative_to(web_dir).as_posix()}:{info.st_mtime_ns}:{info.st_size}\n".encode())
    return digest.hexdigest()[:16]


@app.get("/")
def home():
    revision = _frontend_revision()
    markup = (web_dir / "index.html").read_text(encoding="utf-8")
    markup = markup.replace('="/static/', f'="/_ui/{revision}/')
    return HTMLResponse(markup, headers={"Cache-Control": "no-store", "X-UI-Version": revision})


@app.get("/app.js")
def script():
    return FileResponse(web_dir / "app.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache, max-age=0, must-revalidate"})


@app.get("/health")
@app.get("/healthz")
def health():
    return {"status": "ok", "version": "1.0.0"}


@app.get("/api/v1/capabilities", dependencies=[Depends(require_key)])
def capabilities():
    return capabilities_body()


@app.post("/ui/v1/session")
async def login(request: Request, response: Response):
    _same_origin(request)
    try:
        payload = await request.json()
        key = payload.get("api_key", "") if isinstance(payload, dict) else ""
    except ValueError:
        raise HTTPException(400, "Некорректный запрос")
    if not isinstance(key, str) or not key_valid(key):
        raise HTTPException(401, "Неверный ключ доступа")
    _set_session_cookie(response, request)
    return capabilities_body()


@app.get("/ui/v1/session")
def session(request: Request, response: Response):
    if settings.public_ui_access and not _has_session(request):
        _set_session_cookie(response, request)
    elif not _has_session(request):
        raise HTTPException(401, "Войдите с ключом доступа")
    return capabilities_body()


@app.delete("/ui/v1/session", dependencies=[Depends(require_ui)])
def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.get("/ui/v1/health", dependencies=[Depends(require_ui)])
def ui_health():
    return {"ok": True, **capabilities_body()}


@app.get("/api/v1/model-health", dependencies=[Depends(require_key)])
@app.get("/ui/v1/model-health", dependencies=[Depends(require_ui)])
def model_health():
    if settings.analysis_backend != "datathon":
        return {"ready": None, "message": "Включён локальный детектор, Qwen не подключён."}
    from .datathon import check_health
    return check_health(settings.datathon_api_url, settings.datathon_api_key)


def _create_ready_run(*args, **kwargs):
    # Check outside SQLite's write lock. Existing idempotent requests must still
    # return their original job, even when the model has subsequently gone down.
    health = model_health() if settings.analysis_backend == "datathon" else {"ready": True}
    try:
        return db.create_run(*args, **kwargs, allow_create=health["ready"] is True)
    except db.CreationUnavailableError:
        raise HTTPException(503, health["message"], headers={"Retry-After": "10", "X-Model-State": health["code"]})


@app.get("/ui/v1/catalog", dependencies=[Depends(require_ui)])
def catalog():
    body = catalog_payload()
    body["analysis_scope"] = capabilities_body()["analysis_scope"]
    known_rules = body["analysis_scope"]["checked_rule_ids"]
    for rule in body["rules"]:
        rule["local_check_available"] = rule["id"] in known_rules
    return body


def _header_looks_video(header, extension):
    if extension in (".mov", ".qt", ".mp4", ".m4v"):
        return header[4:8] in (b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip")
    if extension == ".avi":
        return header[:4] == b"RIFF" and header[8:12] == b"AVI "
    return header[:4] == b"\x1a\x45\xdf\xa3"


async def _accept(file, request, origin):
    accepted_at = time.time()
    if file is None:
        raise HTTPException(400, "Передайте видео в поле file")
    filename = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    extension = Path(filename).suffix.lower()
    if extension not in settings.formats():
        raise HTTPException(415, "Поддерживаются: " + ", ".join(settings.formats()))
    key = request.headers.get("Idempotency-Key")
    if key and (len(key) > 128 or not key.isascii()):
        raise HTTPException(400, "Некорректный ключ идемпотентности")
    if not key:
        with db.connect(settings.data_dir) as conn:
            active = conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','processing') AND deleted_at IS NULL").fetchone()[0]
        if active >= settings.max_queue:
            raise HTTPException(503, "Очередь заполнена")
    destination = settings.data_dir / "uploads" / (uuid.uuid4().hex + extension)
    count, digest = 0, hashlib.sha256()
    try:
        with destination.open("wb") as stream:
            while chunk := await file.read(1024 * 1024):
                count += len(chunk)
                if count > settings.max_upload_bytes:
                    raise HTTPException(413, "Файл превышает допустимый размер")
                digest.update(chunk)
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if count < 12:
            raise HTTPException(400, "Файл пустой или повреждён")
        with destination.open("rb") as stream:
            if not _header_looks_video(stream.read(12), extension):
                raise HTTPException(415, "Содержимое файла не похоже на видео выбранного формата")
        snapshot = {"pipeline": "oko", "analysis_backend": settings.analysis_backend, "detectors": capabilities_body()["detectors"], "max_queue": settings.max_queue,
                    "analysis_scope": capabilities_body()["analysis_scope"],
                    "threshold": settings.threshold, "review_threshold": settings.review_threshold,
                    "sample_fps": settings.sample_fps, "version": "1.0.0", "deadline_seconds": settings.deadline_seconds,
                    "max_video_seconds": settings.max_video_seconds}
        fingerprint = hashlib.sha256((digest.hexdigest() + json.dumps(snapshot, sort_keys=True)).encode()).hexdigest()
        try:
            result = await run_in_threadpool(_create_ready_run, settings.data_dir, destination, filename, VIDEO_MIME[extension],
                                   digest.hexdigest(), key, fingerprint, origin, snapshot, accepted_at=accepted_at)
        except db.QueueFullError as exc:
            raise HTTPException(503, str(exc))
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        return {"job_id": result["job_id"], "status": "processing"}
    finally:
        await file.close()
        destination.unlink(missing_ok=True)


@app.post("/api/v1/analyze", dependencies=[Depends(require_key)])
async def analyze(request: Request, file: UploadFile | None = File(default=None)):
    return await _accept(file, request, "api")


@app.post("/ui/v1/analyze", dependencies=[Depends(require_ui)])
async def ui_analyze(request: Request, file: UploadFile | None = File(default=None)):
    return await _accept(file, request, "ui")


@app.get("/api/v1/jobs/{job_id}", dependencies=[Depends(require_key)])
def status(job_id: str):
    return _jury(_run(job_id))


@app.get("/ui/v1/jobs/{job_id}", dependencies=[Depends(require_ui)])
def ui_status(job_id: str):
    return _run(job_id)


@app.get("/ui/v1/jobs/{job_id}/api", dependencies=[Depends(require_ui)])
def ui_api_result(job_id: str):
    return _jury(_run(job_id))


@app.get("/ui/v1/runs", dependencies=[Depends(require_ui)])
def runs(search: str = "", status: str = "", sort: str = "desc", limit: int = 20, offset: int = 0, review_status: str = ""):
    try:
        result = db.list_runs(settings.data_dir, search[:250], status, sort, limit, offset, review_status=review_status)
        result["items"] = [_public_dates(run) for run in result["items"]]
        return result
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/ui/v1/runs/{job_id}", dependencies=[Depends(require_ui)])
def run_detail(job_id: str):
    return _run(job_id)


@app.patch("/ui/v1/runs/{job_id}/review", dependencies=[Depends(require_ui)])
async def review(job_id: str, request: Request):
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("Ожидается объект просмотра")
        run = db.patch_review(settings.data_dir, job_id, payload)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc))
    if run is None:
        raise _not_found()
    return run


@app.post("/ui/v1/runs/{job_id}/save", dependencies=[Depends(require_ui)])
def save(job_id: str):
    try:
        run = db.retry_save(settings.data_dir, job_id)
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc))
    if run is None:
        raise _not_found()
    return _run(job_id)


@app.post("/ui/v1/runs/{job_id}/reanalyze", dependencies=[Depends(require_ui)])
def reanalyze(job_id: str, request: Request):
    run = _run(job_id)
    asset = run.get("asset")
    if not asset or not asset["available"]:
        raise HTTPException(409, "Оригинал видео недоступен")
    # Reuse the stored binary by copying to a temporary intake file; create_run deduplicates it.
    import shutil
    key = request.headers.get("Idempotency-Key")
    if not key or len(key) > 128:
        raise HTTPException(400, "Для нового запуска нужен Idempotency-Key")
    with db.asset_reader(settings.data_dir, job_id) as path:
        destination = settings.data_dir / "uploads" / (uuid.uuid4().hex + path.suffix)
        shutil.copyfile(path, destination)
    try:
        snapshot = {"pipeline": "oko", "analysis_backend": settings.analysis_backend, "detectors": capabilities_body()["detectors"], "max_queue": settings.max_queue,
                    "analysis_scope": capabilities_body()["analysis_scope"],
                    "threshold": settings.threshold, "review_threshold": settings.review_threshold,
                    "sample_fps": settings.sample_fps, "version": "1.0.0", "deadline_seconds": settings.deadline_seconds,
                    "max_video_seconds": settings.max_video_seconds}
        content_hash = asset.get("content_hash")
        if not content_hash:
            with destination.open("rb") as stream:
                content_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        fingerprint = hashlib.sha256((content_hash + json.dumps(snapshot, sort_keys=True)).encode()).hexdigest()
        result = _create_ready_run(settings.data_dir, destination, run["filename"], asset["mime_type"],
                               content_hash, key, fingerprint, "ui", snapshot)
        return {"job_id": result["job_id"], "status": "processing"}
    except db.QueueFullError as exc:
        raise HTTPException(503, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    finally:
        destination.unlink(missing_ok=True)


@app.delete("/ui/v1/runs/{job_id}", dependencies=[Depends(require_ui)])
def delete(job_id: str):
    db.delete_run(settings.data_dir, job_id)
    return {"ok": True}


class LeasedVideoResponse(FileResponse):
    async def __call__(self, scope, receive, send):
        try:
            with db.asset_reader(settings.data_dir, self.run_id) as path:
                self.path = str(path)
                self.stat_result = None
                await super().__call__(scope, receive, send)
        except FileNotFoundError:
            from starlette.responses import JSONResponse
            await JSONResponse({"detail": "Видео недоступно"}, status_code=404)(scope, receive, send)


@app.api_route("/ui/v1/runs/{job_id}/video", methods=["GET", "HEAD"], dependencies=[Depends(require_ui)])
def video(job_id: str):
    run = _run(job_id)
    if not run.get("asset") or not run["asset"]["available"]:
        raise HTTPException(404, "Видео недоступно")
    response = LeasedVideoResponse("", media_type=run["asset"].get("mime_type") or "video/mp4",
                                   filename=run["filename"], content_disposition_type="inline",
                                   headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
    response.run_id = job_id
    return response
