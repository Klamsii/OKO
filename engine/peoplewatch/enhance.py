"""Deterministic, temporally smoothed image preparation. No generative restoration."""
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

from cashwatch.video import write_json


@dataclass(frozen=True)
class Settings:
    mode: str = 'auto'
    gamma: float = 1.0
    contrast: float = 1.0
    clahe: float = 0.0
    denoise: float = 0.0
    sharpen: float = 0.0
    smoothing: float = 0.2

    def __post_init__(self):
        if self.mode not in ('original', 'auto', 'custom'):
            raise ValueError('Unknown enhancement mode')
        for name, low, high in [('gamma', .6, 1.4), ('contrast', .8, 1.3),
                               ('clahe', 0, 3), ('denoise', 0, 5),
                               ('sharpen', 0, .5), ('smoothing', .01, 1)]:
            value = getattr(self, name)
            if type(value) not in (int, float) or not np.isfinite(value) or not low <= value <= high:
                raise ValueError(f'{name} must be between {low} and {high}')


def quality(rgb):
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    small = cv2.resize(gray, (320, max(1, round(gray.shape[0] * 320 / gray.shape[1]))))
    p10, p90 = np.percentile(gray, [10, 90])
    return {'brightness': float(gray.mean()), 'dark_fraction': float((gray < 20).mean()),
            'clipped_fraction': float((gray > 250).mean()), 'contrast_span': float(p90 - p10),
            'sharpness_proxy': float(cv2.Laplacian(small, cv2.CV_32F).var())}


def automatic(q):
    # y = x ** gamma; gamma < 1 brightens. No auto denoise/sharpen without validation.
    return {'gamma': .82 if q['brightness'] < 75 else 1., 'contrast': 1.,
            'clahe': 1.5 if q['contrast_span'] < 90 else 0., 'denoise': 0., 'sharpen': 0.}


def transform(rgb, params):
    if all(params[k] == v for k, v in [('gamma', 1), ('contrast', 1), ('clahe', 0),
                                       ('denoise', 0), ('sharpen', 0)]):
        return rgb.copy()
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)
    light = lab[:, :, 0].astype(np.float32) / 255
    light = np.clip(((light ** params['gamma']) - .5) * params['contrast'] + .5, 0, 1)
    light = np.round(light * 255).astype(np.uint8)
    if params['clahe'] >= .1:
        adjusted = cv2.createCLAHE(clipLimit=max(1., params['clahe']), tileGridSize=(8, 8)).apply(light)
        # Fade CLAHE in/out to avoid an on/off discontinuity in adaptive mode.
        light = cv2.addWeighted(light, 1 - min(1., params['clahe']), adjusted, min(1., params['clahe']), 0)
    if params['denoise'] > 0:
        light = cv2.fastNlMeansDenoising(light, None, params['denoise'], 7, 21)
    if params['sharpen'] > 0:
        light = cv2.addWeighted(light, 1 + params['sharpen'], cv2.GaussianBlur(light, (0, 0), 1), -params['sharpen'], 0)
    lab[:, :, 0] = light
    return cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)


def read_rgb(path):
    with Image.open(path) as im:
        return np.array(ImageOps.exif_transpose(im).convert('RGB'))


def prepare_images(video, directory, settings):
    signature = hashlib.sha256(json.dumps({'version': 1, **asdict(settings)}, sort_keys=True).encode()).hexdigest()[:12]
    root = Path(directory) / f'enhancement-{signature}'
    root.mkdir(parents=True, exist_ok=True)
    selected = [f for f in video['frames'] if f['path']]
    seed = [quality(read_rgb(f['path'])) for f in selected if f['seconds'] <= 2][:7]
    profile = automatic({k: float(np.median([q[k] for q in seed])) for k in seed[0]})
    rows = []; previous_time = selected[0]['seconds']
    for frame in selected:
        rgb = read_rgb(frame['path']); q = quality(rgb)
        if settings.mode == 'auto':
            target = automatic(q)
            alpha = 1 - (1 - settings.smoothing) ** max(0., frame['seconds'] - previous_time)
            profile = {k: profile[k] + alpha * (target[k] - profile[k]) for k in profile}
            params = dict(profile)
        elif settings.mode == 'custom':
            params = {k: getattr(settings, k) for k in profile}
        else:
            params = dict(gamma=1., contrast=1., clahe=0., denoise=0., sharpen=0.)
        dest = root / f"{frame['index']:08}.png"
        if settings.mode == 'original':
            dest = Path(frame['path'])
        elif not dest.exists():
            Image.fromarray(transform(rgb, params)).save(dest)
        frame.update(original_path=frame['path'], path=str(dest), enhancement=params,
                     enhancement_mode=settings.mode, quality=q, image_size=[rgb.shape[1], rgb.shape[0]])
        rows.append({k: frame[k] for k in ('index', 'seconds', 'original_path', 'path', 'quality', 'enhancement')})
        previous_time = frame['seconds']
    write_json(root / 'manifest.json', {'settings': asdict(settings), 'signature': signature, 'frames': rows})
    video['enhancement'] = {'signature': signature, 'settings': asdict(settings), 'manifest': str(root / 'manifest.json')}
    return video


def crop_original(frame, box, destination, margin=.25):
    rgb = read_rgb(frame.get('original_path', frame['path']))
    height, width = rgb.shape[:2]
    x1, y1, x2, y2 = box
    if not (0 <= x1 < x2 <= 1000 and 0 <= y1 < y2 <= 1000):
        raise ValueError('Invalid normalized crop box')
    dx, dy = (x2 - x1) * margin, (y2 - y1) * margin
    bounds = [max(0, int((x1-dx)*width/1000)), max(0, int((y1-dy)*height/1000)),
              min(width, int(np.ceil((x2+dx)*width/1000))), min(height, int(np.ceil((y2+dy)*height/1000)))]
    a, b, c, d = bounds
    crop = rgb[b:d, a:c]
    params = (automatic(quality(crop)) if frame.get('enhancement_mode', 'auto') == 'auto'
              else dict(frame['enhancement']))
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(transform(crop, params)).save(destination)
    return {'path': str(destination), 'bounds_pixels': bounds, 'source_size': [width, height], 'enhancement': params}


def restore_box(box, crop):
    a, b, c, d = crop['bounds_pixels']; width, height = crop['source_size']
    return [(a + box[0] * (c-a)/1000)*1000/width, (b + box[1]*(d-b)/1000)*1000/height,
            (a + box[2]*(c-a)/1000)*1000/width, (b + box[3]*(d-b)/1000)*1000/height]
