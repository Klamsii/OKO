import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    rules_path: Path
    api_key: str
    max_upload_bytes: int
    max_video_seconds: float
    model_timeout_seconds: int
    retention_hours: int
    worker_concurrency: int
    model_command: tuple[str, ...]
    ffmpeg: str
    ffprobe: str
    deadline_seconds: float = 240.0
    reserve_seconds: float = 20.0
    max_queue: int = 50
    sample_fps: float = 5.0
    threshold: float = 0.5
    review_threshold: float = 0.3
    detectors: tuple[str, ...] = ("obstruction",)
    face_blur: str = "auto"
    embedded_worker: bool = False
    enable_team_model: bool = False
    session_hours: int = 12
    analysis_backend: str = "local"
    datathon_api_url: str = ""
    datathon_api_key: str = ""
    public_ui_access: bool = False

    def active_detectors(self):
        return ("datathon",) if self.analysis_backend == "datathon" else self.detectors + (("team",) if self.enable_team_model else ())

    def formats(self):
        return (".mov",) if self.analysis_backend == "datathon" else (".mov", ".qt", ".mp4", ".m4v", ".avi", ".mkv", ".webm")

    def pipeline_settings(self):
        from .oko.config import Settings as PipelineSettings
        return PipelineSettings(
            data_dir=self.data_dir, api_keys=(), workers=self.worker_concurrency,
            deadline_sec=self.deadline_seconds, reserve_sec=self.reserve_seconds,
            max_upload_mb=max(1, self.max_upload_bytes // (1024 * 1024)),
            max_queue=self.max_queue, sample_fps=self.sample_fps,
            threshold=self.threshold, review_threshold=self.review_threshold,
            detectors=self.detectors + (("team",) if self.enable_team_model else ()),
            keep_videos=True, face_blur=self.face_blur, evidence_max_width=1280,
            jpeg_quality=85, api_extra_fields=False, max_violations=10,
            team_model_command=self.model_command, team_rules_path=str(self.rules_path),
            model_timeout_seconds=self.model_timeout_seconds,
            max_video_seconds=self.max_video_seconds,
        )

    @classmethod
    def from_env(cls) -> "Settings":
        backend = os.getenv("ANALYSIS_BACKEND", "local")
        if backend not in ("local", "datathon"):
            raise RuntimeError("ANALYSIS_BACKEND: local или datathon")
        remote_url = os.getenv("DATATHON_API_URL", "").rstrip("/")
        if backend == "datathon":
            from .datathon import validate_url
            validate_url(remote_url)
        key = os.getenv("API_KEY", "") or os.getenv("OKO_API_KEYS", "").split(",")[0].strip()
        if len(key) < 24 or key == "replace-with-a-long-random-secret-key":
            raise RuntimeError("Задайте уникальный API_KEY длиной не менее 24 символов")
        raw_command = os.getenv("MODEL_COMMAND_JSON", "[]")
        try:
            command = json.loads(raw_command)
        except json.JSONDecodeError as exc:
            raise RuntimeError("MODEL_COMMAND_JSON должен быть JSON массивом") from exc
        if not isinstance(command, list) or any(not isinstance(part, str) or not part for part in command):
            raise RuntimeError("MODEL_COMMAND_JSON должен быть массивом строк")
        if command and command[0] in ("python", "python3", "py"):
            command[0] = sys.executable
        detectors = tuple(x.strip() for x in os.getenv("OKO_DETECTORS", "obstruction").split(",") if x.strip())
        if not detectors or any(x != "obstruction" for x in detectors):
            raise RuntimeError("В production доступны OKO_DETECTORS=obstruction; модель команды включается ENABLE_TEAM_MODEL=1")
        sample_fps = float(os.getenv("OKO_SAMPLE_FPS", "5"))
        threshold = float(os.getenv("OKO_THRESHOLD", "0.5"))
        review_threshold = float(os.getenv("OKO_REVIEW_THRESHOLD", "0.3"))
        if not (0 < sample_fps <= 30 and 0 <= review_threshold <= threshold <= 1):
            raise RuntimeError("Некорректные частота кадров или пороги уверенности")
        return cls(
            data_dir=Path(os.getenv("DATA_DIR", "data")).resolve(),
            rules_path=Path(os.getenv("RULES_PATH", "rules.json")).resolve(),
            api_key=key,
            max_upload_bytes=min(256 if backend == "datathon" else 4096, max(1, int(os.getenv("MAX_UPLOAD_MB", "256")))) * 1024 * 1024,
            max_video_seconds=float(os.getenv("MAX_VIDEO_SECONDS", "3600")),
            model_timeout_seconds=int(os.getenv("MODEL_TIMEOUT_SECONDS", "210")),
            retention_hours=int(os.getenv("RETENTION_HOURS", "24")),
            worker_concurrency=max(1, int(os.getenv("WORKER_CONCURRENCY", "1"))),
            model_command=tuple(command),
            ffmpeg=os.getenv("FFMPEG_BIN", "ffmpeg"),
            ffprobe=os.getenv("FFPROBE_BIN", "ffprobe"),
            deadline_seconds=max(5, min(900 if backend == "datathon" else 300,
                                        float(os.getenv("OKO_DEADLINE_SEC", "900" if backend == "datathon" else "240")))),
            reserve_seconds=max(1, float(os.getenv("OKO_RESERVE_SEC", "20"))),
            max_queue=max(1, int(os.getenv("OKO_MAX_QUEUE", "2" if backend == "datathon" else "50"))),
            sample_fps=sample_fps, threshold=threshold, review_threshold=review_threshold,
            detectors=detectors, face_blur=os.getenv("OKO_FACE_BLUR", "auto"),
            embedded_worker=os.getenv("EMBEDDED_WORKER", "1") == "1",
            enable_team_model=os.getenv("ENABLE_TEAM_MODEL", "0") == "1",
            session_hours=max(1, int(os.getenv("SESSION_HOURS", "12"))),
            analysis_backend=backend, datathon_api_url=remote_url,
            datathon_api_key=os.getenv("DATATHON_API_KEY", "") or key,
            public_ui_access=os.getenv("PUBLIC_UI_ACCESS", "0") == "1",
        )
