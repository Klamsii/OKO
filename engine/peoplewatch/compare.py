"""Run an explicit same-source original/auto comparison without claiming ground truth."""
import argparse
import json
from pathlib import Path

from .enhance import Settings, prepare_images
from .hybrid import HybridDetector
from .pipeline import analyze
from .review import generate
from .video import prepare


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('video', type=Path)
    parser.add_argument('--output', type=Path, default=Path('D:/DATATHON/peoplewatch_comparison'))
    parser.add_argument('--yolo-weights', type=Path, default=Path('D:/DATATHON/models/yolo11n.pt'))
    args = parser.parse_args()
    detector = HybridDetector(args.yolo_weights)
    reports = []
    for mode in ('original', 'auto'):
        directory, video = prepare(args.video, args.output)
        prepare_images(video, directory, Settings(mode=mode))
        reports.append(analyze(directory, video, detector)['report_path'])
    result = generate(reports, args.output / 'review')
    (args.output / 'reports.json').write_text(json.dumps(reports, indent=2), 'utf-8')
    print(result)


if __name__ == '__main__':
    main()
