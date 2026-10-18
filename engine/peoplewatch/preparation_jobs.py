"""Background CPU frame preparation; no model import, inference or upload server."""
import argparse
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import uuid


SCHEMA = 1
SETTINGS = {'sample_fps': [1, 3], 'max_seconds': None, 'image_format': 'PNG',
            'color': 'RGB', 'enhancement': 'original', 'timestamps': 'original_pts'}
VIDEO_SUFFIXES = {'.mov', '.mp4', '.m4v', '.avi', '.mkv', '.webm', '.mpeg', '.mpg', '.mts'}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _write(path, value):
    """Readers see the previous complete JSON or the next complete JSON."""
    path = Path(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _read(path):
    return json.loads(Path(path).read_text('utf-8'))


def preparer_signature():
    """Invalidate caches when decoding/sampling implementation or dependencies change."""
    base = Path(__file__).resolve().parents[1]
    files = ('peoplewatch/preparation_jobs.py', 'peoplewatch/video.py',
             'cashwatch/video.py', 'cashwatch/planning.py')
    versions = {}
    for package in ('av', 'Pillow'):
        try: versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError: versions[package] = 'not-installed'
    return {'schema': SCHEMA, 'files': {name: _sha(base / name) for name in files},
            'packages': versions}


def _identity(digest, signature, settings):
    return hashlib.sha256(json.dumps({'source_sha256': digest, 'preparer': signature,
        'settings': settings}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _job_path(root, job_id):
    if not isinstance(job_id, str) or len(job_id) != 64 or any(c not in '0123456789abcdef' for c in job_id):
        raise ValueError('Invalid preparation job id')
    return Path(root).resolve() / job_id


def _inside(path, root):
    path = Path(path).resolve(strict=True)
    if not path.is_relative_to(Path(root).resolve()):
        raise ValueError('Prepared artifact escapes its attempt directory')
    return path


def _stable_sha(path):
    before = path.stat()
    digest = _sha(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise RuntimeError('Source changed while hashing; finish the upload first')
    if not after.st_size:
        raise ValueError('Empty video')
    return digest, after.st_size


def _validate_manifest(request, manifest_path, snapshot, attempt):
    """Technical readiness: all requested original-PTS samples exist and are readable."""
    from PIL import Image
    from cashwatch.planning import sample_indices
    manifest_path = _inside(manifest_path, attempt)
    snapshot = _inside(snapshot, attempt)
    if _sha(snapshot) != request['source_sha256']:
        raise ValueError('Source snapshot SHA does not match the submitted bytes')
    video = _read(manifest_path)
    if (video.get('sha256') != request['source_sha256']
            or Path(video['source']).resolve() != snapshot):
        raise ValueError('Video manifest identifies another source')
    if (video.get('source_complete') is not True or video.get('analysis_truncated') is not False
            or video.get('analysis_limit_seconds') is not None
            or video.get('max_seconds') is not None or video.get('decode_error') or video.get('read_error')
            or video.get('source_kind') == 'image'):
        raise ValueError('Partial, failed or non-video preparation cannot be ready')
    frames = video.get('frames')
    if not isinstance(frames, list) or not frames:
        raise ValueError('No prepared video frame metadata')
    origin = video['source_start_seconds']
    if not isinstance(origin, (int, float)) or not math.isfinite(origin):
        raise ValueError('Missing source timestamp origin')
    times, hashes = [], {}
    for n, frame in enumerate(frames):
        seconds = frame['seconds']
        base = Fraction(frame['time_base'])
        if (frame['index'] != n or type(frame['pts']) is not int or base <= 0
                or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0
                or (times and seconds <= times[-1])
                or not math.isclose(seconds, float(frame['pts'] * base) - origin, abs_tol=1e-6)):
            raise ValueError('Invalid or non-original frame timestamps')
        times.append(seconds)
        if frame.get('path'):
            image = _inside(frame['path'], attempt)
            with Image.open(image) as opened:
                if opened.format != 'PNG' or opened.width <= 0 or opened.height <= 0:
                    raise ValueError('Prepared sample must be a readable PNG')
                opened.verify()
            hashes[str(image)] = _sha(image)
    required = set()
    for fps in request['settings']['sample_fps']:
        required.update(sample_indices(times, fps))
    if any(not frames[n].get('path') for n in required):
        raise ValueError('A required 1/3 FPS sample is missing')
    return video, hashes


def _verified_ready(job, status):
    request = _read(job / status['request'])
    if request['preparer'] != preparer_signature() or request['settings'] != SETTINGS:
        raise ValueError('Preparer version/settings changed')
    if _identity(request['source_sha256'], request['preparer'], request['settings']) != job.name:
        raise ValueError('Preparation identity mismatch')
    attempt = _inside(job / request['attempt'], job)
    manifest = _inside(status['manifest'], attempt)
    integrity_path = _inside(status['integrity'], attempt)
    if _sha(manifest) != status['manifest_sha256'] or _sha(integrity_path) != status['integrity_sha256']:
        raise ValueError('Prepared manifest/integrity file changed')
    _, images = _validate_manifest(request, manifest, status['source_snapshot'], attempt)
    if images != _read(integrity_path)['images_sha256']:
        raise ValueError('A prepared image changed since validation')
    return dict(status, cache_valid=True)


def get_preparation_status(root, job_id):
    """Uploader polling hook. `ready` is returned only after integrity validation."""
    job = _job_path(root, job_id)
    if not job.exists():
        raise FileNotFoundError('Unknown preparation job')
    try:
        status = _read(job / 'status.json')
    except FileNotFoundError:
        if (job / 'worker.lock').exists():
            return {'job_id': job_id, 'status': 'queued', 'phase': 'reserving', 'cache_valid': False}
        raise
    if status['status'] == 'ready':
        try: return _verified_ready(job, status)
        except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
            return dict(status, status='failed', phase='cache_validation', cache_valid=False, error=str(exc))
    return dict(status, cache_valid=False)


def _spawn_worker(root, job_id, token, log_path):
    environment = dict(os.environ, CUDA_VISIBLE_DEVICES='', PYTHONIOENCODING='utf-8')
    options = dict(cwd=str(Path(__file__).resolve().parents[1]), env=environment,
                   stdin=subprocess.DEVNULL, stderr=subprocess.STDOUT, close_fds=True)
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options['start_new_session'] = True
    with log_path.open('ab') as log:
        return subprocess.Popen([sys.executable, '-u', '-m', 'peoplewatch.preparation_jobs', '_worker',
            '--root', str(root), '--job-id', job_id, '--token', token], stdout=log, **options)


def start_preparation(source, root):
    """After upload finalization: deduplicate by bytes, reserve, then launch one CPU worker."""
    source = Path(source).resolve(strict=True)
    if not source.is_file() or source.suffix.lower() not in VIDEO_SUFFIXES:
        raise ValueError('Expected an existing video file')
    digest, size = _stable_sha(source)
    signature = preparer_signature()
    job_id = _identity(digest, signature, SETTINGS)
    job = _job_path(root, job_id)
    job.mkdir(parents=True, exist_ok=True)
    lock, token = job / 'worker.lock', uuid.uuid4().hex
    try:
        with lock.open('x', encoding='utf-8') as stream:
            json.dump({'token': token, 'pid': os.getpid(), 'reserved_utc': _now()}, stream)
    except FileExistsError:
        return dict(get_preparation_status(root, job_id), reused=True,
                    submitted_source=str(source), submitted_name=source.name)
    handed_off = False
    try:
        if (job / 'status.json').exists():
            status = get_preparation_status(root, job_id)
            if status['status'] == 'ready':
                return dict(status, reused=True, submitted_source=str(source), submitted_name=source.name)
        attempt = 'attempts/' + uuid.uuid4().hex
        (job / attempt).mkdir(parents=True)
        request_path = Path(attempt) / 'request.json'
        request = dict(schema=SCHEMA, job_id=job_id, source=str(source), source_name=source.name, source_sha256=digest,
                       source_bytes=size, preparer=signature, settings=SETTINGS, attempt=attempt)
        _write(job / request_path, request)
        status = dict(schema=SCHEMA, job_id=job_id, status='queued', phase='waiting_for_cpu_worker',
                      source_sha256=digest, original_source=str(source), original_name=source.name,
                      source_bytes=size, request=request_path.as_posix(), created_utc=_now(),
                      updated_utc=_now(), progress_percent=None, settings=SETTINGS)
        _write(job / 'status.json', status)
        _spawn_worker(job.parent, job_id, token, job / attempt / 'worker.log')
        handed_off = True
        return dict(status, reused=False, cache_valid=False,
                    submitted_source=str(source), submitted_name=source.name)
    except Exception as exc:
        _write(job / 'status.json', dict(job_id=job_id, status='failed', phase='launch',
                                       updated_utc=_now(), error=str(exc), cache_valid=False))
        raise
    finally:
        if not handed_off:
            lock.unlink(missing_ok=True)


def run_worker(root, job_id, token):
    """Private subprocess entry point; safe to call with a mocked decoder in CPU tests."""
    job = _job_path(root, job_id)
    lock = job / 'worker.lock'
    if _read(lock).get('token') != token:
        raise RuntimeError('Worker does not own this preparation reservation')
    status = _read(job / 'status.json')
    request = _read(job / status['request'])
    attempt = _inside(job / request['attempt'], job)
    # Even a duplicated private invocation with the same token must not decode twice.
    with (attempt / 'execution.lock').open('x', encoding='utf-8') as reservation:
        json.dump({'pid': os.getpid(), 'started_utc': _now()}, reservation)
    _write(lock, {'token': token, 'pid': os.getpid(), 'started_utc': _now()})
    def update(state='preparing', **values):
        status.update(status=state, updated_utc=_now(), worker_pid=os.getpid(), **values)
        _write(job / 'status.json', status)
    try:
        if request['preparer'] != preparer_signature():
            raise RuntimeError('Preparer changed after submission; submit again')
        update(phase='copying_source', copied_bytes=0)
        snapshot = attempt / 'source.mov'
        temporary = snapshot.with_suffix('.part')
        digest, copied = hashlib.sha256(), 0
        with Path(request['source']).open('rb') as original, temporary.open('xb') as target:
            for chunk in iter(lambda: original.read(4 * 1024 * 1024), b''):
                digest.update(chunk); target.write(chunk); copied += len(chunk)
                update(phase='copying_source', copied_bytes=copied, source_bytes=request['source_bytes'])
            target.flush(); os.fsync(target.fileno())
        if digest.hexdigest() != request['source_sha256'] or copied != request['source_bytes']:
            raise RuntimeError('Source changed after submission; no frames were prepared')
        temporary.replace(snapshot)
        update(phase='decoding_frames', source_snapshot=str(snapshot))
        from .video import prepare
        directory, _ = prepare(snapshot, attempt / 'prepared')
        update(phase='validating_frames')
        manifest = Path(directory) / 'video.json'
        video, images = _validate_manifest(request, manifest, snapshot, attempt)
        if request['preparer'] != preparer_signature():
            raise RuntimeError('Preparer changed while decoding; submit with the new version')
        integrity = attempt / 'integrity.json'
        _write(integrity, {'source_sha256': request['source_sha256'], 'images_sha256': images})
        update('ready', phase='ready', manifest=str(manifest), manifest_sha256=_sha(manifest),
               integrity=str(integrity), integrity_sha256=_sha(integrity), frames_saved=len(images),
               native_frames=len(video['frames']), progress_percent=100,
               analysis_end_seconds=video['frames'][-1]['seconds'])
    except BaseException as exc:
        update('failed', phase=status.get('phase', 'preparing'), error=repr(exc), progress_percent=None)
        raise
    finally:
        if lock.exists() and _read(lock).get('token') == token:
            lock.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    start = commands.add_parser('start')
    start.add_argument('source', type=Path)
    start.add_argument('--root', type=Path, required=True)
    status = commands.add_parser('status')
    status.add_argument('job_id')
    status.add_argument('--root', type=Path, required=True)
    worker = commands.add_parser('_worker', help=argparse.SUPPRESS)
    worker.add_argument('--root', type=Path, required=True)
    worker.add_argument('--job-id', required=True)
    worker.add_argument('--token', required=True)
    args = parser.parse_args()
    if args.command == '_worker':
        run_worker(args.root, args.job_id, args.token)
        return
    result = (start_preparation(args.source, args.root) if args.command == 'start'
              else get_preparation_status(args.root, args.job_id))
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
