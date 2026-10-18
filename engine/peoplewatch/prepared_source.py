"""Consume a verified background-prepared video without decoding it again."""
import json
import os
from pathlib import Path
import shutil
from copy import deepcopy

from cashwatch.video import write_json
from .preparation_jobs import _sha, _stable_sha, get_preparation_status


def load_prepared(source, root, job_id):
    """Return (frame directory, manifest) only for these exact original bytes."""
    source = Path(source).resolve(strict=True)
    digest, _ = _stable_sha(source)
    status = get_preparation_status(root, job_id)
    if status['status'] != 'ready' or not status['cache_valid']:
        raise RuntimeError('Background preparation is not verified ready: '
                           + status.get('status', 'unknown'))
    if digest != status['source_sha256']:
        raise ValueError('Prepared job belongs to a different source video')
    manifest = Path(status['manifest']).resolve(strict=True)
    video = json.loads(manifest.read_text('utf-8'))
    if video['sha256'] != digest or Path(video['source']).resolve() != Path(status['source_snapshot']).resolve():
        raise ValueError('Prepared manifest/source identity changed')
    return manifest.parent, video


def materialize_prepared(video, destination):
    """Link/copy already decoded PNGs into a report tree; never decode the MOV.

    Keeping evidence images under the new run lets verified exports remain
    portable without modifying or depending on the shared preparation cache.
    """
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    frames_dir = destination / 'frames'
    frames_dir.mkdir()
    copy = deepcopy(video)
    for frame in copy['frames']:
        if not frame.get('path'):
            continue
        source = Path(frame['path']).resolve(strict=True)
        target = frames_dir / f"{frame['index']:08d}.png"
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)
        if _sha(target) != _sha(source):
            raise ValueError('Materialized evidence image differs from the prepared sample')
        frame['path'] = str(target)
        frame.pop('original_path', None)
    write_json(destination / 'video.json', copy)
    return destination, copy
