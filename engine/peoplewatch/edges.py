"""Experimental boundary views. These are auxiliary observations, not new evidence."""
import argparse
import csv
import html
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

from cashwatch.planning import sample_indices
from cashwatch.video import write_json
from .enhance import read_rgb


def edge_views(rgb, low=60, high=140):
    if not 0 <= low < high <= 255:
        raise ValueError('Canny thresholds require 0 <= low < high <= 255')
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 1)
    edges = cv2.Canny(gray, low, high, L2gradient=True)
    glow = cv2.addWeighted(edges, 1, cv2.GaussianBlur(edges, (0, 0), 1.2), .6, 0)
    overlay = rgb.astype(np.float32)
    alpha = (glow.astype(np.float32) / 255 * .35)[:, :, None]
    overlay = np.round(overlay * (1-alpha) + 255 * alpha).astype(np.uint8)
    return {'edges': cv2.cvtColor(glow, cv2.COLOR_GRAY2RGB), 'overlay': overlay}


def main():
    from .hybrid import HybridDetector
    from .video import prepare
    parser = argparse.ArgumentParser(description='Experimental same-frame original/edges/overlay comparison')
    parser.add_argument('video', type=Path)
    parser.add_argument('--output', type=Path, default=Path('D:/DATATHON/peoplewatch_edges'))
    parser.add_argument('--yolo-weights', type=Path, default=Path('D:/DATATHON/models/yolo11n.pt'))
    parser.add_argument('--low', type=int, default=60)
    parser.add_argument('--high', type=int, default=140)
    args = parser.parse_args()
    if not 0 <= args.low < args.high <= 255:
        parser.error('Expected 0 <= low < high <= 255')
    directory, video = prepare(args.video, args.output)
    root = Path(directory) / f'edges-{args.low}-{args.high}'; root.mkdir(exist_ok=True)
    detector = HybridDetector(args.yolo_weights)
    results = []
    body = ['<!doctype html><meta charset="utf-8"><title>Оригинал и контуры</title>',
            '<style>body{font:16px system-ui;margin:20px}table{width:100%;border-collapse:collapse}'
            'td,th{padding:8px;border:1px solid #aaa;vertical-align:top}img{width:100%;max-width:480px}</style>',
            '<h1>Оригинал, свечение контуров и наложение</h1>'
            '<p>Ответы YOLO не являются ручной разметкой. Контурные обнаружения не добавляются к основному подсчёту.</p>'
            '<table><tr><th>Время</th><th>Оригинал</th><th>Контуры</th><th>Наложение 35%</th></tr>']
    for index in sample_indices([f['seconds'] for f in video['frames']], 1):
        frame = video['frames'][index]; rgb = read_rgb(frame['path'])
        record = {'frame': index, 'seconds': frame['seconds'], 'variants': {}}
        body.append(f'<tr><td>{frame["seconds"]:.3f} с</td>')
        for name, data in {'original': rgb, **edge_views(rgb, args.low, args.high)}.items():
            image_path = root / f'{index:08}-{name}.png'
            Image.fromarray(data).save(image_path)
            detections = detector.detect(image_path)
            record['variants'][name] = {'path': str(image_path), 'detections': detections,
                                        'people_count': sum(o['label'] == 'person' for o in detections)}
            image = Image.fromarray(data); image.thumbnail((640, 960)); draw = ImageDraw.Draw(image)
            for obj in detections:
                box = obj['bbox']; w, h = image.size
                coords = [box[0]*w/1000, box[1]*h/1000, box[2]*w/1000, box[3]*h/1000]
                draw.rectangle(coords, outline='lime', width=2)
                draw.text(coords[:2], obj['label'], fill='yellow', stroke_width=1, stroke_fill='black')
            preview = root / f'{index:08}-{name}-boxes.jpg'; image.save(preview, quality=90)
            count = record['variants'][name]['people_count']
            body.append(f'<td><img loading="lazy" src="{preview.name}"><p>Людей: {count}</p></td>')
        body.append('</tr>'); results.append(record)
        print(f"[edges] {frame['seconds']:.3f}: " + str({k:v['people_count'] for k,v in record['variants'].items()}), flush=True)
    body.append('</table>'); (root / 'index.html').write_text(''.join(body), 'utf-8')
    write_json(root / 'results.json', {'source': video['source'], 'sha256': video['sha256'],
        'model': detector.metadata, 'settings': {'low': args.low, 'high': args.high}, 'frames': results,
        'note': 'Experimental detector responses; not accuracy metrics or confirmed identities.'})
    with (root / 'counts.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f); writer.writerow(['Seconds','Original','Edges','Overlay'])
        for row in results:
            writer.writerow([row['seconds']] + [row['variants'][v]['people_count'] for v in ('original','edges','overlay')])
    print(root / 'index.html')


if __name__ == '__main__':
    main()
