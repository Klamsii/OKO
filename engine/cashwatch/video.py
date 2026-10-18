import hashlib
import json
from pathlib import Path


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def decode_video(source, output_root, max_seconds=None):
    import av
    source = Path(source).resolve(strict=True)
    with source.open('rb') as handle:
        digest = hashlib.file_digest(handle, 'sha256').hexdigest()
    suffix = 'full' if max_seconds is None else f'first-{max_seconds:g}s'
    directory = Path(output_root).resolve() / f'{source.stem}-{digest[:12]}-{suffix}'
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / 'video.json'
    if manifest.exists():
        meta = json.loads(manifest.read_text('utf-8'))
        if all(Path(f['path']).is_file() for f in meta['frames']):
            return directory, meta
    frame_dir = directory / 'frames'
    frame_dir.mkdir(exist_ok=True)
    frames, first_pts = [], None
    with av.open(str(source)) as container:
        if not container.streams.video:
            raise ValueError('No video stream')
        stream = container.streams.video[0]
        for index, frame in enumerate(container.decode(stream)):
            if frame.pts is None or frame.time_base is None:
                raise ValueError('A frame has no source timestamp; refusing invented timecodes')
            pts_seconds = float(frame.pts * frame.time_base)
            if first_pts is None:
                first_pts = pts_seconds
            seconds = pts_seconds - first_pts
            if max_seconds is not None and seconds >= max_seconds:
                break
            if frames and seconds <= frames[-1]['seconds']:
                raise ValueError('Non-increasing video timestamps')
            path = frame_dir / f'{index:08}.png'
            frame.to_image().convert('RGB').save(path)
            frames.append({'index': index, 'seconds': seconds, 'pts': frame.pts,
                           'time_base': str(frame.time_base), 'source_seconds': pts_seconds,
                           'path': str(path)})
        if not frames:
            raise ValueError('No decodable frames in requested range')
        meta = {'source': str(source), 'sha256': digest, 'width': stream.width,
                'height': stream.height, 'source_average_rate': str(stream.average_rate),
                'source_start_seconds': first_pts, 'analysis_limit_seconds': max_seconds,
                'last_frame_seconds': frames[-1]['seconds'], 'frame_count': len(frames),
                'frames': frames}
    write_json(manifest, meta)
    return directory, meta
