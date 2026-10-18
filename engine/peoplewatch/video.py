import hashlib
import json
import math
from pathlib import Path

from cashwatch.planning import sample_indices
from cashwatch.video import write_json


def source_is_partial(video):
    """Old limited manifests remain partial unless end-of-source was recorded."""
    if video.get('analysis_truncated') is True or video.get('source_complete') is False:
        return True
    limit=video.get('analysis_limit_seconds',video.get('max_seconds'))
    if limit is None: return False
    return not (video.get('analysis_truncated') is False and video.get('source_complete') is True)


def prepare(source, output, max_seconds=None):
    import av
    source = Path(source).resolve(strict=True)
    with source.open('rb') as f:
        digest = hashlib.file_digest(f, 'sha256').hexdigest()
    suffix = 'full' if max_seconds is None else f'first-{max_seconds:g}s'
    directory = Path(output).resolve() / f'{source.stem}-{digest[:12]}-{suffix}'
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / 'video.json'
    if manifest.exists():
        data = json.loads(manifest.read_text('utf-8'))
        if all(Path(f['path']).exists() for f in data['frames'] if f['path']):
            return directory, data
    images = directory / 'frames'
    images.mkdir(exist_ok=True)
    if source.suffix.lower() in ('.jpg', '.jpeg', '.png', '.bmp', '.webp', '.tif', '.tiff'):
        from PIL import Image, ImageOps
        path = images / '00000000.png'
        with Image.open(source) as image:
            ImageOps.exif_transpose(image).convert('RGB').save(path)
        data = {'source': str(source), 'sha256': digest, 'source_start_seconds': 0,
                'source_average_rate': None, 'analysis_limit_seconds': None, 'source_kind': 'image',
                'analysis_truncated': False, 'source_complete': True, 'source_duration_seconds': None,
                'frames': [{'index': 0, 'seconds': 0., 'pts': 0, 'time_base': '1', 'path': str(path)}]}
        write_json(manifest, data)
        return directory, data
    frames, start, last = [], None, None
    truncated=False
    targets = {1: 0., 3: 0.}
    with av.open(str(source)) as container:
        if not container.streams.video:
            raise ValueError('Video stream not found')
        stream = container.streams.video[0]
        source_duration=float(stream.duration*stream.time_base) if stream.duration is not None else None
        for frame in container.decode(stream):
            if frame.pts is None:
                raise ValueError('Missing original timestamp')
            absolute = float(frame.pts * frame.time_base)
            start = absolute if start is None else start
            seconds = absolute - start
            if max_seconds is not None and seconds >= max_seconds:
                truncated=True
                break
            if frames and seconds <= frames[-1]['seconds']:
                raise ValueError('Non-increasing timestamps')
            selected = False
            for fps in targets:
                if seconds + 1e-9 >= targets[fps]:
                    selected = True
                    targets[fps] = (math.floor(seconds * fps + 1e-8) + 1) / fps
            path = images / f'{len(frames):08}.png'
            if selected:
                frame.to_image().convert('RGB').save(path)
            frames.append({'index': len(frames), 'seconds': seconds, 'pts': frame.pts,
                           'time_base': str(frame.time_base), 'path': str(path) if selected else None})
            last = frame
        if not frames:
            raise ValueError('No video frames')
        if frames[-1]['path'] is None:
            path = images / f'{len(frames) - 1:08}.png'
            last.to_image().convert('RGB').save(path)
            frames[-1]['path'] = str(path)
        data = {'source': str(source), 'sha256': digest, 'source_start_seconds': start,
                'source_average_rate': str(stream.average_rate), 'frames': frames,
                'analysis_limit_seconds': max_seconds, 'analysis_truncated': truncated,
                'source_complete': not truncated, 'source_duration_seconds': source_duration,
                'analysis_end_seconds': frames[-1]['seconds']}
    times = [f['seconds'] for f in frames]
    for i in set(sample_indices(times, 1) + sample_indices(times, 3)):
        if not frames[i]['path']:
            raise ValueError('Sampling/decoding grid mismatch')
    write_json(manifest, data)
    return directory, data
