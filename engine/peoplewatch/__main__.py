import argparse
import json
from pathlib import Path

from .detector import QwenPeopleDetector
from .pipeline import analyze
from .video import prepare


def main():
    parser = argparse.ArgumentParser(description='1 FPS + 3 FPS people analysis, exactly one image per Qwen request')
    parser.add_argument('video', type=Path)
    parser.add_argument('--model', type=Path, default=Path('D:/DATATHON/Qwen3-VL-8B-Instruct'))
    parser.add_argument('--output', type=Path, default=Path('D:/DATATHON/peoplewatch_results'))
    parser.add_argument('--max-seconds', type=float, help='Partial technical check only')
    parser.add_argument('--backend', choices=['qwen', 'yolo', 'hybrid'], default='hybrid')
    parser.add_argument('--enhance', choices=['original', 'auto', 'custom'], default='auto')
    parser.add_argument('--config', type=Path, help='JSON with enhancement settings and optional normalized zones')
    parser.add_argument('--yolo-weights', type=Path, default=Path('D:/DATATHON/models/yolo11n.pt'))
    parser.add_argument('--max-region-reviews', type=int, default=8)
    parser.add_argument('--edge-review', action='store_true', help='Experimental extra edge view for reviewed regions only')
    parser.add_argument('--no-inventory', action='store_true', help='Disable safe and tabletop inventory')
    parser.add_argument('--inventory-variant',choices=['legacy','fused','memory','memory_verified'],default='memory')
    parser.add_argument('--actions',action='store_true',help='Also analyze cash handling and safe door events')
    args = parser.parse_args()
    if not args.video.is_file():
        parser.error('Video does not exist')
    if args.max_seconds is not None and not 0 < args.max_seconds < float('inf'):
        parser.error('--max-seconds must be a finite positive number')
    if args.max_region_reviews < 0:
        parser.error('--max-region-reviews cannot be negative')
    if args.actions and args.backend=='yolo':
        parser.error('--actions requires Qwen or hybrid backend')
    if args.backend != 'qwen' and not args.yolo_weights.is_file():
        parser.error('YOLO weights missing; run setup_people.ps1 first or specify --yolo-weights')
    config = json.loads(args.config.read_text('utf-8-sig')) if args.config else {}
    zones = config.get('zones', [])
    for index, zone in enumerate(zones):
        zone.setdefault('id', f'Z{index+1:03}')
        box = zone['bbox']
        if (len(box) != 4 or not (0 <= box[0] < box[2] <= 1000 and 0 <= box[1] < box[3] <= 1000)
                or not isinstance(zone.get('label'), str) or not zone['id'].isalnum()):
            parser.error('Zone requires label, alphanumeric ID and bbox normalized to 0..1000')
    if len({z['id'] for z in zones}) != len(zones):
        parser.error('Zone IDs must be unique')
    print('Подготовка исходного видео: выборка 1 и 3 кадра/с...', flush=True)
    directory, video = prepare(args.video, args.output, args.max_seconds)
    from .enhance import Settings, prepare_images
    settings = Settings(**{**config.get('enhancement', {}), 'mode': args.enhance})
    print('Подготовка изображений...', flush=True)
    prepare_images(video, directory, settings)
    qwen = None
    if args.backend in ('qwen', 'hybrid'):
        print('Загрузка локальной Qwen3-VL...', flush=True)
        qwen = QwenPeopleDetector(args.model)
        qwen.model.generation_config.prompt_lookup_num_tokens=8
        qwen.model.generation_config.max_matching_ngram_size=4
    if args.backend == 'qwen':
        detector = qwen
    else:
        from .hybrid import HybridDetector
        detector = HybridDetector(args.yolo_weights, qwen, edge_review=args.edge_review)
    analyze(directory, video, detector, args.max_region_reviews, zones,
            inventory_model=qwen if not args.no_inventory else None,
            inventory_variant=args.inventory_variant,actions_model=qwen if args.actions else None)


if __name__ == '__main__':
    main()
