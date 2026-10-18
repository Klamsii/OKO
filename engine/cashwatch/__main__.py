import argparse
from pathlib import Path

from .detector import QwenCashDetector
from .pipeline import analyze, announce
from .video import decode_video


def main():
    parser = argparse.ArgumentParser(description='Local two-pass cash entry/exit detection with Qwen3-VL')
    parser.add_argument('videos', type=Path, nargs='+')
    parser.add_argument('--model', type=Path, default=Path('D:/DATATHON/Qwen3-VL-8B-Instruct'))
    parser.add_argument('--output', type=Path, default=Path('D:/DATATHON/cashwatch_results'))
    parser.add_argument('--fps', type=float, default=5)
    parser.add_argument('--padding', type=float, default=2, help='Refinement padding/expansion in seconds')
    parser.add_argument('--batch-size', type=int, default=4)
    parser.add_argument('--min-present', type=int, default=2)
    parser.add_argument('--min-absent', type=int, default=2)
    parser.add_argument('--max-seconds', type=float, help='Explicit partial run for smoke testing only')
    parser.add_argument('--refine-all', action='store_true', help='Check every native frame, ignoring coarse speedup')
    args = parser.parse_args()
    if any(not p.is_file() for p in args.videos):
        parser.error('Every input video must exist')
    if min(args.fps, args.padding, args.batch_size, args.min_present, args.min_absent) <= 0:
        parser.error('Numeric settings must be positive')
    if args.max_seconds is not None and args.max_seconds <= 0:
        parser.error('--max-seconds must be positive')
    detector = None
    for video in args.videos:
        announce(stage='decode', video=str(video))
        directory, metadata = decode_video(video, args.output, args.max_seconds)
        if detector is None:
            announce(stage='loading', model=str(args.model))
            detector = QwenCashDetector(args.model)
            announce(stage='loaded')
        analyze(directory, metadata, detector, args.fps, args.padding, args.batch_size,
                args.min_present, args.min_absent, args.refine_all)


if __name__ == '__main__':
    main()
