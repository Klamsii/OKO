"""Local side-by-side audit; detection counts are not ground truth."""
import argparse
import csv
import html
import json
from pathlib import Path

from PIL import Image, ImageDraw


def generate(reports, output):
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    runs = []
    for path in reports:
        path = Path(path)
        report = json.loads(path.read_text('utf-8'))
        rows = json.loads((path.parent / 'observations.json').read_text('utf-8'))
        manifest = json.loads(Path(report['enhancement']['manifest']).read_text('utf-8'))
        runs.append((report, {r['frame']: r for r in rows}, {r['index']: r for r in manifest['frames']}))
    if len({r[0]['source_sha256'] for r in runs}) != 1:
        raise ValueError('Comparison requires the same source')
    common = sorted(set.intersection(*(set(rows) for _, rows, _ in runs)))
    body = ['<!doctype html><meta charset="utf-8"><title>Проверка обработки кадров</title>',
            '<style>body{font:16px system-ui;margin:24px;background:#f5f6fa}table{border-collapse:collapse;width:100%}'
            'td,th{border:1px solid #ccc;padding:10px;vertical-align:top}img{width:100%;max-width:640px}'
            '.note{padding:16px;background:#fff3cd}</style>',
            '<h1>Проверка обработки кадров</h1><p class="note">Число обнаружений — ответы моделей, '
            'а не подтверждённое количество людей. Сравнивайте рамки с исходным изображением.</p><table><tr><th>Время</th>']
    for report, _, _ in runs:
        mode = report['enhancement']['settings']['mode']
        body.append(f'<th>{html.escape(mode)} / {html.escape(report["model"].get("backend", "qwen"))}</th>')
    body.append('</tr>'); metrics = []
    for index in common:
        seconds = runs[0][1][index]['seconds']
        body.append(f'<tr><td>{seconds:.3f} с</td>')
        metric = {'frame': index, 'seconds': seconds}
        for n, (report, rows, frames) in enumerate(runs):
            row, frame = rows[index], frames[index]
            with Image.open(frame['path']) as image:
                image = image.convert('RGB'); image.thumbnail((960, 960)); draw = ImageDraw.Draw(image)
                for obj in row.get('objects', row['people']):
                    x1, y1, x2, y2 = obj['bbox']; w, h = image.size
                    box = [x1*w/1000, y1*h/1000, x2*w/1000, y2*h/1000]
                    draw.rectangle(box, outline='lime', width=2)
                    draw.text((box[0], box[1]), obj.get('label', 'person'), fill='yellow', stroke_width=1, stroke_fill='black')
                name = f'{n}-{index:08}.jpg'; image.save(output / name, quality=90)
            body.append(f'<td><img loading="lazy" src="{name}"><p>Людей: {len(row["people"])}; '
                        f'полнота: {row["complete"]}</p><details><summary>Настройки</summary><pre>'
                        f'{html.escape(json.dumps(frame["enhancement"], ensure_ascii=False, indent=2))}</pre></details></td>')
            metric[f'run_{n}_people'] = len(row['people'])
        metrics.append(metric); body.append('</tr>')
    body.append('</table>')
    (output / 'index.html').write_text(''.join(body), 'utf-8')
    with (output / 'comparison.csv').open('w', encoding='utf-8-sig', newline='') as f:
        if metrics:
            writer = csv.DictWriter(f, fieldnames=list(metrics[0])); writer.writeheader(); writer.writerows(metrics)
    return output / 'index.html'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('reports', nargs='+', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(generate(args.reports, args.output))


if __name__ == '__main__':
    main()
