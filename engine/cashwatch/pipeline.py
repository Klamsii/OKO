import csv
import hashlib
import json
import shutil
import time
from dataclasses import asdict
from pathlib import Path

from .detector import parse_response
from .events import Observation, episodes
from .planning import sample_indices, initial_windows, expand_windows
from .video import write_json


def announce(**values):
    print(json.dumps(values, ensure_ascii=True), flush=True)


def analyze(directory, video, detector, fps=5, padding=2, batch_size=4,
            min_present=2, min_absent=2, refine_all=False):
    if fps <= 0 or padding <= 0 or batch_size < 1 or min_present < 1 or min_absent < 1:
        raise ValueError('Rates, padding, batch size and confirmation counts must be positive')
    settings = dict(fps=fps, padding=padding, batch_size=batch_size,
                    min_present=min_present, min_absent=min_absent, refine_all=refine_all)
    signature = hashlib.sha256(json.dumps([detector.signature, settings], sort_keys=True).encode()).hexdigest()
    run = Path(directory) / ('run-' + signature[:12])
    run.mkdir(parents=True, exist_ok=True)
    state_file = run / 'status.json'
    write_json(state_file, {'status': 'running', 'source': video['source'], 'settings': settings})
    cache_dir = run / 'cache'
    cache_dir.mkdir(exist_ok=True)
    audit_path = run / 'responses.jsonl'
    frames = video['frames']
    times = [f['seconds'] for f in frames]
    cache = {}
    started = time.monotonic()
    for path in cache_dir.glob('*.json'):
        row = json.loads(path.read_text('utf-8'))
        cache[(row['stage'], row['frame'])] = row

    def classify(indices, stage):
        pending = [i for i in indices if (stage, i) not in cache]
        for offset in range(0, len(pending), batch_size):
            ids = pending[offset:offset + batch_size]
            tick = time.monotonic()
            raw = detector.classify([frames[i] for i in ids], stage)
            states, error = parse_response(raw, ids)
            audit = {'stage': stage, 'frame_ids': ids, 'raw': raw, 'parse_error': error,
                     'elapsed_seconds': time.monotonic() - tick}
            with audit_path.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(audit, ensure_ascii=False) + '\n')
            for i in ids:
                row = {'stage': stage, 'frame': i, 'seconds': times[i],
                       'state': states[i], 'parse_error': error}
                write_json(cache_dir / f'{stage}-{i:08}.json', row)
                cache[(stage, i)] = row
            announce(stage=stage, processed=offset + len(ids), pending_total=len(pending),
                     last_seconds=round(times[ids[-1]], 6), parse_error=error)
        return {i: Observation(i, times[i], cache[(stage, i)]['state'], stage) for i in indices}

    try:
        coarse = classify(sample_indices(times, fps), 'coarse')
        windows = [(0, len(times) - 1)] if refine_all else initial_windows(times, coarse.values(), padding)
        fine = {}
        while windows:
            fine.update(classify([i for lo, hi in windows for i in range(lo, hi + 1)], 'fine'))
            expanded = expand_windows(times, windows, fine, padding, max(min_present, min_absent))
            if expanded == windows:
                break
            windows = expanded
            announce(stage='expand', windows=windows)
        combined = coarse | fine
        observed = [combined[i] for i in sorted(combined)]
        report = episodes(observed, min_present, min_absent)
        evidence = run / 'evidence'
        evidence.mkdir(exist_ok=True)
        for event in report['episodes']:
            event['evidence'] = {}
            for key, field in [('entry', 'first_visible_frame'), ('last_visible', 'last_visible_frame'),
                               ('exit', 'first_absent_frame')]:
                index = event[field]
                if index is not None:
                    destination = evidence / f"episode-{event['episode']:03}-{key}-{index:08}.png"
                    shutil.copyfile(frames[index]['path'], destination)
                    event['evidence'][key] = str(destination)
            event['open_at_analysis_limit'] = event['open_end'] and video['analysis_limit_seconds'] is not None
        report.update(status='done', source=video['source'], source_sha256=video['sha256'],
                      source_start_seconds=video['source_start_seconds'], settings=settings,
                      model=detector.metadata, source_frame_count=len(frames),
                      coarse_frame_count=len(coarse), fine_frame_count=len(fine),
                      uncertain_frame_count=sum(o.state == 'uncertain' for o in observed),
                      parse_error_frame_count=sum(bool(cache[(o.stage, o.frame)]['parse_error']) for o in observed),
                      fine_windows_seconds=[[times[lo], times[hi]] for lo, hi in windows],
                      analysis_limit_seconds=video['analysis_limit_seconds'],
                      elapsed_seconds=time.monotonic() - started,
                      limitations=[
                          'Model detections, not manually verified truth; no guaranteed first-frame accuracy.',
                          'Cash visible only between coarse samples may be missed entirely.',
                          'Any visible cash counts as one visibility state, not individual banknotes or transfers.',
                          'Unknown frames do not close episodes; confirmation can suppress one-frame events.',
                          'Times come from source PTS relative to the first decoded frame; no audio is used.',
                      ])
        write_json(run / 'result.json', report)
        write_json(run / 'observations.json', [asdict(o) for o in observed])
        with (run / 'events.csv').open('w', encoding='utf-8-sig', newline='') as handle:
            columns = ['episode', 'in_timecode', 'out_timecode', 'in_seconds', 'out_seconds',
                       'first_visible_frame', 'last_visible_frame', 'first_absent_frame',
                       'already_visible_at_start', 'open_end', 'uncertain_frames',
                       'entry_adjacent_frames', 'exit_adjacent_frames']
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(report['episodes'])
        write_json(state_file, {'status': 'done', 'result': str(run / 'result.json'),
                               'episode_count': report['episode_count'],
                               'appearance_count': report['appearance_count'],
                               'disappearance_count': report['disappearance_count']})
        announce(stage='done', report=str(run / 'result.json'), episodes=report['episode_count'],
                 appearances=report['appearance_count'], disappearances=report['disappearance_count'])
        return report, run
    except BaseException as exc:
        write_json(state_file, {'status': 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed',
                               'error': str(exc), 'source': video['source']})
        raise
