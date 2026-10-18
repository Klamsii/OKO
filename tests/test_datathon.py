"""Contract/transport tests, not claims about recognition accuracy."""
import base64
import copy
import hashlib
import io
import json
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import db
from app.config import Settings
from app.datathon import ModelError, _request, check_health, translate, validate_url
from app.worker import process_integrated_job

KEY = "test-site-key-not-a-production-secret"
MODEL_KEY = "test-model-key-not-a-production-secret"
MODEL_ID = "model-job-1"


def test_datathon_budget_covers_model_queue(monkeypatch):
    monkeypatch.setenv("API_KEY", KEY)
    monkeypatch.setenv("ANALYSIS_BACKEND", "datathon")
    monkeypatch.setenv("DATATHON_API_URL", "https://model.example.test")
    monkeypatch.setenv("OKO_DEADLINE_SEC", "900")
    monkeypatch.delenv("OKO_MAX_QUEUE", raising=False)
    settings = Settings.from_env()
    assert settings.deadline_seconds == 900
    assert settings.max_queue == 2


def reports():
    buf = io.BytesIO(); Image.new("RGB", (32, 32), "white").save(buf, format="JPEG")
    frame = {"timestamp_sec": 1., "image_base64": base64.b64encode(buf.getvalue()).decode()}
    compact = dict(job_id=MODEL_ID, status="done", verdict="violation", preliminary=True,
                   assessment_mode="best_effort", decision_summary="Возможное нарушение",
                   violations=[dict(type="possible_cash_split_unresolved", timecode_sec=1.,
                                    description="Тест контракта; не результат модели.", evidence_frames=[frame])])
    detail = dict(job_id=MODEL_ID, status="done", analysis_status="completed", verdict="possible_violation",
                  preliminary=True, review_issues=[dict(code="cash", affects_verdict=True, description="Не виден исход")],
                  moment_assessments=[dict(start_seconds=.5, end_seconds=1.5, verdict="possible_violation",
                                          affects_verdict=True, reason="Тест", sampling_scope="evaluated_frames_only")],
                  violations=[dict(type="possible_cash_split_unresolved", key_moment_seconds=1.,
                                   event_start_seconds=.5, event_end_seconds=1.5,
                                   timeline_status="review_interval_not_confirmed_event",
                                   normative_basis=dict(status="requires_legal_review", act="Тестовая норма"))])
    return compact, detail


@pytest.fixture
def upstream():
    state = {"uploads": [], "requests": [], "reply": reports(), "status": 200, "health": {"ready": True}}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def send(self, code, body):
            raw = json.dumps(body).encode(); self.send_response(code)
            self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw)))
            self.end_headers(); self.wfile.write(raw)
        def do_POST(self):
            state["requests"].append(self.path)
            if self.headers.get("X-API-Key") != MODEL_KEY: return self.send(401,{})
            state["uploads"].append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send(202, {"job_id": MODEL_ID})
        def do_GET(self):
            state["requests"].append(self.path)
            if self.headers.get("X-API-Key") != MODEL_KEY: return self.send(401,{})
            if state["status"] != 200: return self.send(state["status"],{})
            if self.path == "/health": return self.send(200, state["health"])
            self.send(200, state["reply"][1] if self.path.endswith("/report") else state["reply"][0])
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    yield f"http://127.0.0.1:{server.server_port}", state
    server.shutdown(); server.server_close(); thread.join()


def test_original_video_crosses_real_http_worker_and_retains_rich_report(tmp_path, monkeypatch, upstream):
    monkeypatch.setenv("API_KEY", KEY); monkeypatch.setenv("EMBEDDED_WORKER", "0")
    from app import api
    url, state = upstream
    settings = Settings(data_dir=tmp_path/"data", rules_path=Path("rules.json").resolve(), api_key=KEY,
        max_upload_bytes=4*1024*1024, max_video_seconds=20, model_timeout_seconds=20, retention_hours=24,
        worker_concurrency=1, model_command=(), ffmpeg="ffmpeg", ffprobe="ffprobe", deadline_seconds=20,
        analysis_backend="datathon", datathon_api_url=url, datathon_api_key=MODEL_KEY)
    monkeypatch.setattr(api, "settings", settings)
    video = tmp_path/"source.mov"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 30, (64,64))
    for _ in range(60): writer.write(np.zeros((64,64,3),dtype=np.uint8))
    writer.release(); original = video.read_bytes()
    with TestClient(api.app) as client:
        assert client.post("/ui/v1/session", json={"api_key": KEY}).status_code == 200
        assert client.get("/ui/v1/model-health").json()["ready"] is True
        assert client.post("/api/v1/analyze", files={"file":("x.mov",original)}).status_code == 401
        assert client.get("/api/v1/capabilities", headers={"X-API-Key":KEY}).json()["formats"] == [".mov"]
        assert client.post("/ui/v1/analyze", files={"file":("x.mp4",original)}).status_code == 415
        result = client.post("/ui/v1/analyze", files={"file":("x.mov", original)}, headers={"Idempotency-Key":"one"})
        job_id = result.json()["job_id"]
        job = db.claim_next(settings.data_dir); process_integrated_job(job,settings)
        api_result = client.get(f"/api/v1/jobs/{job_id}", headers={"X-API-Key":KEY}).json()
        assert api_result["status"] == "done", api_result
        assert api_result["violations"] == state["reply"][0]["violations"]
        run = client.get(f"/ui/v1/runs/{job_id}").json()
        assert run["result"]["moment_assessments"] == state["reply"][1]["moment_assessments"]
        assert run["result"]["violations"][0]["timeline_status"] == "review_interval_not_confirmed_event"
        assert run["result"]["preliminary"] and run["result"]["violations"][0]["confidence"] is None
        assert run["config_snapshot"]["analysis_backend"] == "datathon"
        assert MODEL_KEY not in json.dumps(run) and url not in json.dumps(run)
        assert len(state["uploads"]) == 1 and original in state["uploads"][0]
        assert client.get(f"/ui/v1/runs/{job_id}/video").content == original
        again = client.post("/ui/v1/analyze", files={"file":("x.mov", original)}, headers={"Idempotency-Key":"one"})
        assert again.json()["job_id"] == job_id and db.claim_next(settings.data_dir) is None
        assert run["result"]["analysis_scope"]["mode"] == "datathon"
        # An outage must not create doomed jobs, lose the saved original, or
        # break replay of an upload whose response was lost before the outage.
        state["health"] = {"ready": False}
        for route, headers in [("/ui/v1/analyze", {}), ("/api/v1/analyze", {"X-API-Key": KEY})]:
            blocked = client.post(route, files={"file": ("x.mov", original)}, headers=headers)
            assert blocked.status_code == 503 and blocked.headers["Retry-After"] == "10"
            assert blocked.headers["X-Model-State"] == "loading"
        retry_route = f"/ui/v1/runs/{job_id}/reanalyze"
        assert client.post(retry_route, headers={"Idempotency-Key": "retry"}).status_code == 503
        again = client.post("/ui/v1/analyze", files={"file": ("x.mov", original)}, headers={"Idempotency-Key": "one"})
        assert again.json()["job_id"] == job_id
        conflict = client.post("/ui/v1/analyze", files={"file": ("x.mov", original+b"changed")}, headers={"Idempotency-Key": "one"})
        assert conflict.status_code == 409
        assert db.claim_next(settings.data_dir) is None
        assert not list((settings.data_dir/"uploads").iterdir())
        assert client.get(f"/ui/v1/runs/{job_id}/video").content == original
        state["health"] = {"ready": True}
        recovered = client.post(retry_route, headers={"Idempotency-Key": "retry"})
        assert recovered.status_code == 200
        state["health"] = {"ready": False}
        assert client.post(retry_route, headers={"Idempotency-Key": "retry"}).json() == recovered.json()
        assert len(state["uploads"]) == 1  # Only the explicitly processed job reached Qwen.


@pytest.mark.parametrize("change", ["time", "jpeg", "verdict", "bounds", "frames"])
def test_invalid_model_output_is_never_clean(change):
    compact, detail = reports()
    if change == "time": compact["violations"][0]["timecode_sec"] = 999
    if change == "jpeg": compact["violations"][0]["evidence_frames"][0]["image_base64"] = "notjpeg"
    if change == "verdict": compact["verdict"] = "no_violation"
    if change == "bounds": detail["violations"][0]["event_start_seconds"] = 1.2
    if change == "frames": compact["violations"][0]["evidence_frames"] = []
    with pytest.raises(ModelError): translate("site",compact,detail,{"duration_sec":2},1)


def test_no_violation_preserves_optional_issues():
    compact, detail = reports(); compact.update(verdict="no_violation", violations=[])
    detail.update(violations=[],optional_review_failures=[dict(question="cash",error="parse_error")])
    payload=translate("site",compact,detail,{"duration_sec":2},1)
    assert payload["api"]["verdict"] == "no_violation" and payload["result"]["degraded"]
    assert payload["result"]["optional_review_failures"] == detail["optional_review_failures"]


def test_health_rejects_wrong_key(upstream):
    url, _ = upstream
    assert check_health(url, "wrong")["code"] == "auth_error"
    assert check_health(url, MODEL_KEY)["ready"] is True


@pytest.mark.parametrize("body,code", [({"ready": False}, "loading"), ({"status": "ok"}, "wrong_endpoint"), ({"ready": "true"}, "wrong_endpoint")])
def test_health_requires_model_contract(upstream, body, code):
    url, state = upstream; state["health"] = body
    result = check_health(url, MODEL_KEY)
    assert result["ready"] is False and result["code"] == code
    assert MODEL_KEY not in json.dumps(result) and url not in json.dumps(result)


@pytest.mark.parametrize("exception,code", [(httpx.ConnectError("private network details"), "unavailable"), (httpx.ReadTimeout("private network details"), "timeout")])
def test_health_transport_categories(monkeypatch, exception, code):
    def fail(*args, **kwargs): raise exception
    monkeypatch.setattr(httpx.Client, "send", fail)
    result = check_health("https://model.example", MODEL_KEY)
    assert result["ready"] is False and result["code"] == code
    assert "private network" not in result["message"]


@pytest.mark.parametrize("url", ["https://key:secret@example.com", "https://example.com?token=secret", "file:///tmp", ""])
def test_credentials_cannot_be_part_of_url(url):
    with pytest.raises(ModelError): validate_url(url)


def test_redirect_never_receives_secret_and_deadline_fails():
    calls=[]
    def handler(req):
        calls.append(str(req.url)); return httpx.Response(302,headers={"Location":"https://different.example/"})
    with httpx.Client(transport=httpx.MockTransport(handler), base_url="https://model.example/", headers={"X-API-Key":MODEL_KEY}) as client:
        with pytest.raises(ModelError): _request(client,"GET","health",time.monotonic()+2)
        assert calls == ["https://model.example/health"]
        with pytest.raises(ModelError): _request(client,"GET","health",time.monotonic()-1)
