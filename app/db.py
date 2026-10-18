"""Persistent analysis runs, private assets and compatibility with legacy jobs."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1


class QueueFullError(ValueError):
    pass


_COLUMNS = {
    'filename': "TEXT NOT NULL DEFAULT ''", 'mime_type': "TEXT NOT NULL DEFAULT 'application/octet-stream'",
    'origin': "TEXT NOT NULL DEFAULT 'api'", 'video_asset_id': 'TEXT REFERENCES video_assets(id)',
    'ui_result_json': 'TEXT', 'config_json': "TEXT NOT NULL DEFAULT '{}'", 'idempotency_key': 'TEXT',
    'fingerprint': 'TEXT', 'stage': "TEXT NOT NULL DEFAULT 'queued'", 'progress': 'REAL NOT NULL DEFAULT 0',
    'archive_status': "TEXT NOT NULL DEFAULT 'partial'", 'review_status': "TEXT NOT NULL DEFAULT 'not_reviewed'",
    'last_playback_sec': 'REAL NOT NULL DEFAULT 0', 'reviewed_event_ids_json': "TEXT NOT NULL DEFAULT '[]'",
    'schema_version': 'INTEGER NOT NULL DEFAULT 1', 'execution_token': 'TEXT', 'revision': 'INTEGER NOT NULL DEFAULT 0',
    'active_worker': 'INTEGER NOT NULL DEFAULT 0', 'persistent': 'INTEGER NOT NULL DEFAULT 0',
    'deleted_at': 'TEXT', 'completed_at': 'TEXT', 'external_job_id': 'TEXT',
    'accepted_at': 'REAL NOT NULL DEFAULT 0',
    'verdict': 'TEXT', 'violation_count': 'INTEGER',
}


class _Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect(data_dir: Path) -> sqlite3.Connection:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(data_dir / 'jobs.sqlite3', timeout=30, factory=_Connection)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=30000')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.create_function('casefold', 1, lambda text: str(text or '').casefold(), deterministic=True)
    return conn


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _load(value, default=None):
    try:
        return json.loads(value) if value is not None else default
    except (ValueError, TypeError):
        return default


def _name(filename):
    return str(filename).replace('\\', '/').rsplit('/', 1)[-1] or 'video.mov'


def _private_path(data_dir, asset_id, filename):
    suffix = Path(_name(filename)).suffix.lower()
    if not re.fullmatch(r'\.[a-z0-9]{1,10}', suffix):
        suffix = '.bin'
    folder = Path(data_dir).resolve() / 'assets'
    folder.mkdir(parents=True, exist_ok=True)
    return folder / (asset_id + suffix)


def _asset(conn, asset_id):
    return conn.execute('SELECT * FROM video_assets WHERE id=?', (asset_id,)).fetchone()


def _availability(asset):
    return 'available' if asset and Path(asset['storage_ref']).is_file() else 'missing'


def _safe_config(value):
    # Settings snapshots must not turn the archive into a credential store.
    if isinstance(value, dict):
        return {k: _safe_config(v) for k, v in value.items()
                if not any(word in k.lower() for word in ('api_key', 'password', 'secret', 'token', 'cookie', 'authorization'))}
    if isinstance(value, (list, tuple)):
        return [_safe_config(v) for v in value]
    return value


def init(data_dir: Path) -> None:
    """Additive migration; old reports and original files are never overwritten."""
    data_dir = Path(data_dir)
    _backup_legacy_database(data_dir)
    with connect(data_dir) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('''CREATE TABLE IF NOT EXISTS video_assets (
            id TEXT PRIMARY KEY, storage_ref TEXT NOT NULL, original_name TEXT NOT NULL,
            mime_type TEXT NOT NULL, byte_size INTEGER NOT NULL DEFAULT 0, content_hash TEXT,
            duration_sec REAL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            availability TEXT NOT NULL DEFAULT 'available', active_readers INTEGER NOT NULL DEFAULT 0)''')
        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS assets_hash ON video_assets(content_hash) WHERE content_hash IS NOT NULL')
        conn.execute('''CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, file_path TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('queued','processing','done','failed')),
            result_json TEXT, error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
        columns = {r['name'] for r in conn.execute('PRAGMA table_info(jobs)')}
        for key, declaration in _COLUMNS.items():
            if key not in columns:
                conn.execute(f'ALTER TABLE jobs ADD COLUMN {key} {declaration}')
        conn.execute("UPDATE jobs SET accepted_at=COALESCE(CAST(strftime('%s',created_at) AS REAL),?) WHERE accepted_at=0", (time.time(),))
        conn.execute('CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status,created_at)')
        conn.execute('CREATE INDEX IF NOT EXISTS jobs_assets ON jobs(video_asset_id)')
        # Deleted runs retain keys: retrying a request must not resurrect history.
        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS jobs_idempotency ON jobs(origin,idempotency_key) WHERE idempotency_key IS NOT NULL')
        conn.execute('CREATE TABLE IF NOT EXISTS archive_imports(source TEXT PRIMARY KEY,job_id TEXT NOT NULL)')
        for row in conn.execute('SELECT * FROM jobs WHERE video_asset_id IS NULL').fetchall():
            path = Path(row['file_path'])
            asset_id = uuid.uuid5(uuid.NAMESPACE_URL, str(data_dir.resolve()) + ':legacy:' + row['id']).hex
            stored = _private_path(data_dir, asset_id, path.name)
            if path.is_file() and path.resolve() != stored and not stored.exists():
                shutil.copy2(path, stored)
            available = stored.is_file()
            conn.execute('''INSERT OR IGNORE INTO video_assets
                (id,storage_ref,original_name,mime_type,byte_size,availability)
                VALUES(?,?,?,'application/octet-stream',?,?)''',
                (asset_id, str(stored), path.name or 'video.mov', stored.stat().st_size if available else 0,
                 'available' if available else 'missing'))
            conn.execute('''UPDATE jobs SET video_asset_id=?,file_path=?,filename=?,origin='legacy',persistent=1,
                stage=status,progress=CASE WHEN status='done' THEN 1 ELSE 0 END,
                active_worker=CASE WHEN status='processing' THEN 1 ELSE 0 END,archive_status=? WHERE id=?''',
                (asset_id, str(stored), path.name, 'saved' if available else 'partial', row['id']))
        _import_oko(conn, data_dir)
        conn.execute("UPDATE jobs SET accepted_at=COALESCE(CAST(strftime('%s',created_at) AS REAL),?) WHERE accepted_at=0", (time.time(),))
        for row in conn.execute("SELECT id,result_json FROM jobs WHERE status='done' AND violation_count IS NULL").fetchall():
            report = _load(row['result_json'], {})
            conn.execute('UPDATE jobs SET verdict=?,violation_count=? WHERE id=?',
                         (report.get('verdict'), len(report.get('violations') or []), row['id']))


def _backup_legacy_database(data_dir: Path) -> None:
    """SQLite backup includes committed WAL data before any migration writes."""
    database = data_dir.resolve() / 'jobs.sqlite3'
    if not database.is_file():
        return
    source = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=30)
    try:
        exists = source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone()
        if not exists or 'video_asset_id' in {r[1] for r in source.execute('PRAGMA table_info(jobs)')}:
            return
        backup_dir = data_dir / 'backups'
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        destination = backup_dir / f'legacy-before-integration-{stamp}-{uuid.uuid4().hex[:8]}.sqlite3'
        temporary = destination.with_suffix('.sqlite3.tmp')
        try:
            target = sqlite3.connect(temporary)
            try:
                source.backup(target)
            finally:
                target.close()
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    finally:
        source.close()


def _import_oko(conn, data_dir):
    for source in sorted((data_dir / 'jobs').glob('*/api.json')):
        identity = 'oko:' + str(source.resolve())
        if conn.execute('SELECT 1 FROM archive_imports WHERE source=?', (identity,)).fetchone():
            continue
        try:
            api = json.loads(source.read_text(encoding='utf-8'))
            ui_file = source.parent / 'ui.json'
            ui = json.loads(ui_file.read_text(encoding='utf-8')) if ui_file.is_file() else None
            if not isinstance(api, dict) or (ui is not None and not isinstance(ui, dict)):
                continue
        except (OSError, ValueError):
            continue
        external = source.parent.name
        run_id = external
        if conn.execute('SELECT 1 FROM jobs WHERE id=?', (run_id,)).fetchone():
            run_id = 'oko-' + uuid.uuid5(uuid.NAMESPACE_URL, identity).hex
        filename = _name((ui or {}).get('filename') or 'video.mov')
        asset_id = uuid.uuid5(uuid.NAMESPACE_URL, identity + ':asset').hex
        stored = _private_path(data_dir, asset_id, filename)
        # Match retained OKO upload by its generated job ID, not a user filename.
        retained = next((p for p in (data_dir / 'uploads').glob(external + '.*') if p.is_file()), None)
        if retained and not stored.exists():
            shutil.copy2(retained, stored)
        available = stored.is_file()
        conn.execute('''INSERT OR IGNORE INTO video_assets
            (id,storage_ref,original_name,mime_type,byte_size,availability)
            VALUES(?,?,?,'application/octet-stream',?,?)''',
            (asset_id, str(stored), filename, stored.stat().st_size if available else 0,
             'available' if available else 'missing'))
        status = api.get('status') if api.get('status') in ('done', 'failed') else 'failed'
        error = (api.get('error') or 'Прерванная проверка прежнего архива.') if status == 'failed' else None
        conn.execute('''INSERT INTO jobs
            (id,file_path,status,result_json,ui_result_json,error,filename,origin,video_asset_id,stage,
             progress,archive_status,persistent,external_job_id,completed_at)
            VALUES(?,?,?,?,?,?,?,'legacy_oko',?,?,?,?,1,?,CURRENT_TIMESTAMP)''',
            (run_id, str(stored), status, _json(api), _json(ui) if ui else None, error, filename,
             asset_id, status, 1 if status == 'done' else 0, 'saved' if available else 'partial', external))
        conn.execute('INSERT INTO archive_imports(source,job_id) VALUES(?,?)', (identity, run_id))


def _discard_temp(conn, path):
    if not conn.execute('SELECT 1 FROM video_assets WHERE storage_ref=?', (str(path.resolve()),)).fetchone():
        path.unlink(missing_ok=True)


def create_run(data_dir: Path, upload_path: Path, filename: str, mime_type: str, content_hash: str,
               idempotency_key: str | None, fingerprint: str, origin: str = 'ui',
               config_snapshot: dict | None = None, accepted_at: float | None = None) -> dict:
    data_dir, upload = Path(data_dir), Path(upload_path).resolve()
    if idempotency_key is not None and (not idempotency_key or len(idempotency_key) > 256):
        raise ValueError('Некорректный ключ идемпотентности.')
    config = _json(_safe_config(config_snapshot or {}))
    moved_to = None
    with connect(data_dir) as conn:
        try:
            conn.execute('BEGIN IMMEDIATE')
            if idempotency_key:
                old = conn.execute('SELECT * FROM jobs WHERE origin=? AND idempotency_key=?',
                                   (origin, idempotency_key)).fetchone()
                if old:
                    previous_asset = _asset(conn, old['video_asset_id'])
                    if (old['fingerprint'] != fingerprint or old['deleted_at'] or old['config_json'] != config
                            or (content_hash and previous_asset['content_hash'] != content_hash.lower())):
                        raise ValueError('Ключ уже использован для другой или удалённой проверки.')
                    _discard_temp(conn, upload)
                    return {'job_id': old['id'], 'created': False}
            max_queue = (config_snapshot or {}).get('max_queue')
            if max_queue is not None:
                active = conn.execute("SELECT COUNT(*) FROM jobs WHERE deleted_at IS NULL AND status IN ('queued','processing')").fetchone()[0]
                if active >= int(max_queue):
                    raise QueueFullError('Очередь заполнена. Повторите позже.')
            if not upload.is_file():
                raise FileNotFoundError('Загруженное видео недоступно.')
            with upload.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            if content_hash and digest != content_hash.lower():
                raise ValueError('Хеш загруженного видео не совпадает.')
            old = conn.execute('SELECT * FROM video_assets WHERE content_hash=?', (digest,)).fetchone()
            if old:
                asset_id, stored = old['id'], Path(old['storage_ref'])
                if not stored.is_file():
                    stored = _private_path(data_dir, asset_id, filename)
                    os.replace(upload, stored)
                    moved_to = stored
                    conn.execute("UPDATE video_assets SET storage_ref=?,byte_size=?,availability='available' WHERE id=?",
                                 (str(stored), stored.stat().st_size, asset_id))
                elif upload != stored.resolve():
                    _discard_temp(conn, upload)
            else:
                asset_id = uuid.uuid4().hex
                stored = _private_path(data_dir, asset_id, filename)
                os.replace(upload, stored)
                moved_to = stored
                conn.execute('''INSERT INTO video_assets
                    (id,storage_ref,original_name,mime_type,byte_size,content_hash) VALUES(?,?,?,?,?,?)''',
                    (asset_id, str(stored), _name(filename), mime_type, stored.stat().st_size, digest))
            run_id = uuid.uuid4().hex
            conn.execute('''INSERT INTO jobs
                (id,file_path,status,filename,mime_type,origin,video_asset_id,idempotency_key,fingerprint,
                 config_json,stage,archive_status,persistent,accepted_at) VALUES(?,?,'queued',?,?,?,?,?,?,?,'queued','saving',?,?)''',
                (run_id, str(stored), _name(filename), mime_type, origin, asset_id, idempotency_key,
                 fingerprint, config, 0 if origin == 'api' else 1, accepted_at if accepted_at is not None else time.time()))
            conn.commit()
            return {'job_id': run_id, 'created': True}
        except Exception:
            conn.rollback()
            if moved_to and moved_to.exists() and not upload.exists():
                os.replace(moved_to, upload)
            raise


def create_job(data_dir: Path, job_id: str, file_path: Path) -> None:
    """Old temporary API adapter; the old caller owns its original path."""
    path = Path(file_path).resolve()
    with connect(data_dir) as conn:
        asset_id = uuid.uuid4().hex
        conn.execute('''INSERT INTO video_assets(id,storage_ref,original_name,mime_type,byte_size,availability)
            VALUES(?,?,?,'application/octet-stream',?,?)''',
            (asset_id, str(path), path.name, path.stat().st_size if path.is_file() else 0,
             'available' if path.is_file() else 'missing'))
        conn.execute("""INSERT INTO jobs(id,file_path,status,filename,video_asset_id,origin,archive_status,accepted_at)
            VALUES(?,?,'queued',?,?,'legacy_api','saving',?)""", (job_id, str(path), path.name, asset_id, time.time()))


def get_job(data_dir: Path, job_id: str) -> dict | None:
    with connect(data_dir) as conn:
        row = conn.execute('SELECT * FROM jobs WHERE id=? AND deleted_at IS NULL', (job_id,)).fetchone()
        return dict(row) if row else None


def claim_next(data_dir: Path) -> dict | None:
    with connect(data_dir) as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute("SELECT id FROM jobs WHERE status='queued' AND deleted_at IS NULL ORDER BY created_at,id LIMIT 1").fetchone()
        if not row:
            return None
        conn.execute("""UPDATE jobs SET status='processing',stage='decode',execution_token=?,revision=revision+1,
            active_worker=1,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='queued'""", (uuid.uuid4().hex, row['id']))
        return dict(conn.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone())


def set_progress(data_dir: Path, job_id: str, stage: str, progress: float, token=None) -> bool:
    progress = float(progress)
    if not math.isfinite(progress):
        raise ValueError('Некорректный прогресс.')
    with connect(data_dir) as conn:
        sql = "UPDATE jobs SET stage=?,progress=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='processing' AND deleted_at IS NULL"
        args = [stage, min(1.0, max(0.0, progress)), job_id]
        if token is not None:
            sql += ' AND execution_token=?'
            args.append(token)
        return conn.execute(sql, args).rowcount > 0


def _ui_body(ui):
    if isinstance(ui, dict) and 'result' in ui and ('job_id' in ui or 'status' in ui):
        return ui.get('result')
    return ui


def _stable_result(job_id, result):
    result = copy.deepcopy(result)
    body = _ui_body(result)
    if isinstance(body, dict):
        body.setdefault('schema_version', SCHEMA_VERSION)
        for group in ('violations', 'review'):
            for index, event in enumerate(body.get(group) or []):
                if isinstance(event, dict):
                    event.setdefault('event_id', uuid.uuid5(uuid.NAMESPACE_URL, f'{job_id}:{group}:{index}').hex)
    return result


def _gc_asset(conn, data_dir, asset_id):
    asset = _asset(conn, asset_id) if asset_id else None
    if not asset or asset['active_readers']:
        return
    needed = conn.execute("""SELECT 1 FROM jobs WHERE video_asset_id=? AND
        (active_worker=1 OR (deleted_at IS NULL AND (persistent=1 OR status IN ('queued','processing')))) LIMIT 1""",
        (asset_id,)).fetchone()
    if needed:
        return
    path = Path(asset['storage_ref'])
    # Never delete original paths outside the managed private asset directory.
    if path.resolve().is_relative_to(Path(data_dir).resolve() / 'assets'):
        path.unlink(missing_ok=True)
        conn.execute("UPDATE video_assets SET availability='missing' WHERE id=?", (asset_id,))
        conn.execute("UPDATE jobs SET archive_status='partial' WHERE video_asset_id=? AND deleted_at IS NULL", (asset_id,))


def _stage_path(data_dir, job_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', job_id):
        raise ValueError('Некорректный ID проверки.')
    folder = Path(data_dir) / 'staged'
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f'{job_id}.json'


def _replace_checkpoint(source, destination):
    # Windows can briefly deny replacement while a polling response reads it.
    for attempt in range(12):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == 11:
                raise
            time.sleep(.02)


def complete_run(data_dir: Path, job_id: str, api_result: dict, ui_result: dict,
                 execution_token=None, *, _from_checkpoint=False) -> bool:
    ui = _stable_result(job_id, ui_result)
    staged = _stage_path(data_dir, job_id)
    with connect(data_dir) as conn:
        row = conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row or (execution_token is not None and row['execution_token'] != execution_token):
            return False
        if row['status'] == 'done' and not row['deleted_at']:
            staged.unlink(missing_ok=True)
            return True
        if row['status'] == 'failed' and not row['deleted_at'] and not _from_checkpoint:
            return False
    if not row['deleted_at']:
        temporary = staged.with_name(staged.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('w', encoding='utf-8') as stream:
                stream.write(_json({'api': api_result, 'result': ui, 'execution_token': execution_token}))
                stream.flush()
                os.fsync(stream.fileno())
            _replace_checkpoint(temporary, staged)
        finally:
            temporary.unlink(missing_ok=True)
    with connect(data_dir) as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row or (execution_token is not None and row['execution_token'] != execution_token):
            return False
        if row['deleted_at']:
            conn.execute("UPDATE jobs SET active_worker=0,status='failed',stage='deleted' WHERE id=?", (job_id,))
            _gc_asset(conn, data_dir, row['video_asset_id'])
            staged.unlink(missing_ok=True)
            return False
        if row['status'] not in ('queued', 'processing') and not (_from_checkpoint and row['stage'] in ('interrupted', 'failed', 'save_failed')):
            return row['status'] == 'done'
        asset = _asset(conn, row['video_asset_id'])
        body = _ui_body(ui) or {}
        duration = (body.get('video') or {}).get('duration_sec')
        if isinstance(duration, (float, int)) and math.isfinite(duration) and duration >= 0:
            conn.execute('UPDATE video_assets SET duration_sec=? WHERE id=?', (duration, row['video_asset_id']))
        conn.execute("""UPDATE jobs SET status='done',result_json=?,ui_result_json=?,error=NULL,stage='done',
            progress=1,archive_status=?,active_worker=0,revision=revision+1,completed_at=CURRENT_TIMESTAMP,
            verdict=?,violation_count=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (_json(api_result), _json(ui), 'saved' if _availability(asset) == 'available' else 'partial',
             body.get('verdict'), len(body.get('violations') or []), job_id))
        _gc_asset(conn, data_dir, row['video_asset_id'])
    staged.unlink(missing_ok=True)
    return True


def finish(data_dir: Path, job_id: str, result: dict) -> None:
    complete_run(data_dir, job_id, result, result)


def fail(data_dir: Path, job_id: str, message: str, execution_token=None) -> bool:
    with connect(data_dir) as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row or (execution_token is not None and row['execution_token'] != execution_token):
            return False
        if row['status'] not in ('queued', 'processing') and not row['deleted_at']:
            return False
        asset = _asset(conn, row['video_asset_id'])
        conn.execute("""UPDATE jobs SET status='failed',error=?,stage='failed',active_worker=0,archive_status=?,
            revision=revision+1,completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (str(message)[:500], 'saved' if _availability(asset) == 'available' else 'partial', job_id))
        _gc_asset(conn, data_dir, row['video_asset_id'])
        return True


def _present(conn, row, full=True, data_dir=None):
    asset = _asset(conn, row['video_asset_id'])
    available = _availability(asset)
    result = _ui_body(_stable_result(row['id'], _load(row['ui_result_json']))) if full else None
    api = _load(row['result_json']) if full else None
    checkpoint = None
    if result is None and data_dir is not None and (full or row['archive_status'] == 'save_failed'):
        try:
            checkpoint = _load((Path(data_dir) / 'staged' / (row['id'] + '.json')).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
        if checkpoint and (checkpoint.get('execution_token') in (None, row['execution_token']) or row['stage'] == 'interrupted'):
            result = _ui_body(_stable_result(row['id'], checkpoint.get('result')))
            api = checkpoint.get('api')
        else:
            checkpoint = None
    if result is None and api is not None:
        result = _stable_result(row['id'], api)
    body = result if isinstance(result, dict) else {}
    archive = row['archive_status']
    if checkpoint:
        archive = 'save_failed'
    if available != 'available' and archive == 'saved':
        archive = 'partial'
    data = {
        'job_id': row['id'], 'id': row['id'], 'status': 'done' if checkpoint else row['status'],
        'analysis_status': 'completed' if checkpoint else {'queued': 'pending', 'processing': 'running', 'done': 'completed', 'failed': 'failed'}.get(row['status'], row['status']),
        'stage': row['stage'], 'progress': row['progress'], 'filename': row['filename'], 'created_at': row['created_at'],
        'updated_at': row['updated_at'], 'completed_at': row['completed_at'], 'error': row['error'], 'origin': row['origin'],
        'archive_status': archive, 'review_status': row['review_status'], 'last_playback_sec': row['last_playback_sec'],
        'reviewed_event_ids': _load(row['reviewed_event_ids_json'], []), 'schema_version': row['schema_version'],
        'revision': row['revision'], 'config_snapshot': _load(row['config_json'], {}),
        'accepted_at': row['accepted_at'],
        'asset': None if not asset else {'id': asset['id'], 'original_name': asset['original_name'],
            'mime_type': asset['mime_type'], 'byte_size': asset['byte_size'], 'duration_sec': asset['duration_sec'],
            'content_hash': asset['content_hash'], 'availability': available},
        'verdict': body.get('verdict') if full else row['verdict'],
        'violation_count': (len(body.get('violations') or []) if full else row['violation_count']) if checkpoint or row['status'] == 'done' else None,
    }
    if full:
        data.update(result=result, api_result=api)
    return data


def get_run(data_dir: Path, job_id: str) -> dict | None:
    with connect(data_dir) as conn:
        row = conn.execute('SELECT * FROM jobs WHERE id=? AND deleted_at IS NULL', (job_id,)).fetchone()
        return _present(conn, row, data_dir=data_dir) if row else None


def list_runs(data_dir: Path, search='', status='', sort='desc', limit=20, offset=0, review_status='') -> dict:
    limit, offset = max(1, min(100, int(limit))), max(0, int(offset))
    clauses, args = ['deleted_at IS NULL', 'persistent=1'], []
    if search:
        clauses.append('instr(casefold(filename),casefold(?))>0')
        args.append(search)
    if review_status:
        if review_status not in ('not_reviewed', 'in_review', 'reviewed'):
            raise ValueError('Неизвестный статус просмотра.')
        clauses.append('review_status=?')
        args.append(review_status)
    if status:
        status = {'pending': 'queued', 'running': 'processing', 'completed': 'done'}.get(status, status)
        column = 'archive_status' if status in ('saving', 'saved', 'partial', 'save_failed') else 'review_status' if status in ('not_reviewed', 'in_review', 'reviewed') else 'status'
        clauses.append(column + '=?')
        args.append(status)
    where = ' AND '.join(clauses)
    order = 'ASC' if sort == 'asc' else 'DESC'
    with connect(data_dir) as conn:
        total = conn.execute('SELECT COUNT(*) FROM jobs WHERE ' + where, args).fetchone()[0]
        columns = ','.join(['id', 'file_path', 'status', 'error', 'created_at', 'updated_at',
                            *(name for name in _COLUMNS if name != 'ui_result_json')])
        rows = conn.execute(f'SELECT {columns} FROM jobs WHERE {where} ORDER BY created_at {order},rowid {order} LIMIT ? OFFSET ?',
                            [*args, limit, offset]).fetchall()
        return {'items': [_present(conn, row, full=False, data_dir=data_dir) for row in rows], 'total': total, 'offset': offset, 'limit': limit}


def patch_review(data_dir: Path, job_id: str, payload: dict) -> dict | None:
    if set(payload) - {'review_status', 'last_playback_sec', 'reviewed_event_ids'}:
        raise ValueError('Неизвестные поля просмотра.')
    with connect(data_dir) as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM jobs WHERE id=? AND deleted_at IS NULL', (job_id,)).fetchone()
        if not row:
            return None
        run = _present(conn, row, data_dir=data_dir)
        status = payload.get('review_status', row['review_status'])
        if status not in ('not_reviewed', 'in_review', 'reviewed'):
            raise ValueError('Неизвестный статус просмотра.')
        position = float(payload.get('last_playback_sec', row['last_playback_sec']))
        if not math.isfinite(position) or position < 0:
            raise ValueError('Некорректная позиция видео.')
        duration = (run['asset'] or {}).get('duration_sec')
        if duration is not None:
            position = min(position, duration)
        events = payload.get('reviewed_event_ids', run['reviewed_event_ids'])
        if not isinstance(events, list) or any(not isinstance(e, str) for e in events):
            raise ValueError('Некорректные идентификаторы эпизодов.')
        body = run.get('result') or {}
        known = {e.get('event_id') for group in ('violations', 'review') for e in body.get(group, []) if isinstance(e, dict)}
        if set(events) - known:
            raise ValueError('Эпизод не принадлежит этой проверке.')
        conn.execute('''UPDATE jobs SET review_status=?,last_playback_sec=?,reviewed_event_ids_json=?,
            revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE id=?''',
            (status, position, _json(list(dict.fromkeys(events))), job_id))
        return _present(conn, conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone(), data_dir=data_dir)


def delete_run(data_dir: Path, job_id: str) -> bool:
    with connect(data_dir) as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            return False
        conn.execute('''UPDATE jobs SET deleted_at=COALESCE(deleted_at,CURRENT_TIMESTAMP),revision=revision+1,
            updated_at=CURRENT_TIMESTAMP WHERE id=?''', (job_id,))
    # The deletion intent must be committed before deleting the only binary copy.
    with connect(data_dir) as conn:
        conn.execute('BEGIN IMMEDIATE')
        _gc_asset(conn, data_dir, row['video_asset_id'])
    _stage_path(data_dir, job_id).unlink(missing_ok=True)
    return True


@contextmanager
def asset_reader(data_dir: Path, job_id: str):
    """Yield a Path and keep its lease until the streaming response closes."""
    with connect(data_dir) as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM jobs WHERE id=? AND deleted_at IS NULL', (job_id,)).fetchone()
        asset = _asset(conn, row['video_asset_id']) if row else None
        if _availability(asset) != 'available':
            raise FileNotFoundError('Видео недоступно.')
        asset_id, path = asset['id'], Path(asset['storage_ref'])
        conn.execute('UPDATE video_assets SET active_readers=active_readers+1 WHERE id=?', (asset_id,))
    try:
        yield path
    finally:
        with connect(data_dir) as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('UPDATE video_assets SET active_readers=MAX(0,active_readers-1) WHERE id=?', (asset_id,))
            _gc_asset(conn, data_dir, asset_id)


def release_worker(data_dir: Path, job_id: str, execution_token=None) -> bool:
    """Release a worker's file lease in finally, including tombstoned runs."""
    with connect(data_dir) as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row or (execution_token is not None and row['execution_token'] != execution_token):
            return False
        conn.execute('UPDATE jobs SET active_worker=0 WHERE id=?', (job_id,))
        _gc_asset(conn, data_dir, row['video_asset_id'])
        return True


def retry_save(data_dir: Path, job_id: str) -> dict | None:
    run = get_run(data_dir, job_id)
    if run is None:
        return None
    path = _stage_path(data_dir, job_id)
    if path.is_file():
        staged = json.loads(path.read_text(encoding='utf-8'))
        token = None if run['stage'] == 'interrupted' else staged.get('execution_token')
        complete_run(data_dir, job_id, staged['api'], staged['result'], token, _from_checkpoint=True)
    elif run['archive_status'] in ('saving', 'save_failed'):
        raise ValueError('Копия отчёта для повтора сохранения недоступна.')
    return get_run(data_dir, job_id)


def mark_save_failed(data_dir: Path, job_id: str) -> bool:
    """Best-effort status after a worker has durably staged a completed report."""
    if not _stage_path(data_dir, job_id).is_file():
        return False
    with connect(data_dir) as conn:
        return conn.execute("""UPDATE jobs SET archive_status='save_failed',updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND deleted_at IS NULL AND status!='done'""", (job_id,)).rowcount > 0


def recover_interrupted(data_dir: Path) -> None:
    """Call once at worker startup, never while another server owns live readers."""
    with connect(data_dir) as conn:
        conn.execute('BEGIN IMMEDIATE')
        rows = conn.execute("SELECT * FROM jobs WHERE status='processing' OR active_worker=1").fetchall()
        conn.execute("""UPDATE jobs SET status='failed',stage='interrupted',active_worker=0,execution_token=NULL,
            error='Обработка прервана перезапуском сервера.',revision=revision+1,updated_at=CURRENT_TIMESTAMP
            WHERE status='processing' OR active_worker=1""")
        conn.execute('UPDATE video_assets SET active_readers=0')
        for row in rows:
            _gc_asset(conn, data_dir, row['video_asset_id'])


def purge_expired(data_dir: Path, hours: int) -> int:
    """Only temporary API runs expire. UI and migrated archives are persistent."""
    if hours <= 0:
        return 0
    with connect(data_dir) as conn:
        conn.execute('BEGIN IMMEDIATE')
        rows = conn.execute("""SELECT * FROM jobs WHERE persistent=0 AND status IN ('done','failed')
            AND active_worker=0 AND deleted_at IS NULL AND updated_at < datetime('now',?)""", (f'-{int(hours)} hours',)).fetchall()
        for row in rows:
            conn.execute('UPDATE jobs SET deleted_at=CURRENT_TIMESTAMP,revision=revision+1 WHERE id=?', (row['id'],))
            _gc_asset(conn, data_dir, row['video_asset_id'])
        return len(rows)
