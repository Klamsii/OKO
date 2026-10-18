import csv
import hashlib
import json
import shutil
import time
from bisect import bisect_left, bisect_right
from pathlib import Path

from cashwatch.planning import sample_indices, merge_windows
from cashwatch.video import write_json
from .detector import parse_people
from .tracking import track_people
from .scene import build_scene
from .reflections import reconcile_reflections

ROLES = {'employee': 'Сотрудник', 'client': 'Клиент', 'unknown': 'Роль не определена'}


def refinement_windows(times, triggers):
    windows = []
    for index in triggers:
        start = max(times[0], times[index] - 1)
        end = min(times[-1], start + 3)
        windows.append((bisect_left(times, start), bisect_right(times, end) - 1))
    return merge_windows(windows)


def display(report):
    print('\n=== РЕЗУЛЬТАТ: ЛЮДИ В КАДРЕ ===', flush=True)
    print(f"Видео: {report['source']}")
    if report['analysis_limit_seconds'] is not None:
        print(f"ЧАСТИЧНАЯ ПРОВЕРКА: первые {report['analysis_limit_seconds']:g} секунд")
    count = report['initial_count']
    print(f"На первом кадре: {count if count is not None else 'число не определено'} чел.")
    print(f"Сотрудников: {report['employee_count']}; клиентов: {report['client_count']}; "
          f"без определённой роли: {report['unknown_count']}")
    for track in report['tracks']:
        print(f"\n{track['id']} — {ROLES[track['role']]}")
        for visit in track['visits']:
            label = 'уже в кадре' if visit['already_present'] else 'появился'
            end = visit['out_timecode'] or 'уход не подтверждён к концу просмотра'
            print(f"  {label}: {visit['in_timecode']}; ушёл: {end}")
    print('\nРоли назначены по заданному правилу, не установлены фактически.')
    print('Количество — оценка по визуальным траекториям; при потере связи возможен повторный учёт.')
    print('Уточнение 3 кадра/с: шаг около 0,333 с, а не точность каждого исходного кадра.')
    if report['incomplete_observations']:
        print(f"Неясных наблюдений: {report['incomplete_observations']}; они не подтверждают уход.")
    print(f"Отчёт: {report['report_path']}", flush=True)
    if report.get('inventory_path'):
        print(f"Сейфы и предметы на столах ({report['inventory_frames']} кадров): {report['inventory_path']}", flush=True)
        print(f"Таблица предметов: {Path(report['inventory_path']).with_suffix('.csv')}", flush=True)
    if report.get('enhancement'):
        print(f"Обработка: {report['enhancement']['settings']['mode']}; проверок областей: {report['region_reviews']}")
        print(f"Журнал объектов: {report['scene_path']}")
        if report.get('deferred_region_reviews'):
            print(f"Не выполнено проверок областей из-за лимита: {report['deferred_region_reviews']}")


def analyze(directory, video, detector, max_region_reviews=8, zones=None, inventory_model=None,
            inventory_variant='memory', actions_model=None):
    started = time.perf_counter()
    signature = hashlib.sha256(json.dumps([detector.signature, 'people-tracker-v3-reflections',
        video.get('enhancement'), max_region_reviews, zones or [],
        ['inventory-memory-v1',inventory_variant] if inventory_model else None,
        'actions-v1' if actions_model else None], sort_keys=True).encode()).hexdigest()[:12]
    run = Path(directory) / f'run-{signature}'
    run.mkdir(parents=True, exist_ok=True)
    cache = run / 'cache'
    cache.mkdir(exist_ok=True)
    frames = video['frames']
    times = [f['seconds'] for f in frames]
    observations = {}
    inventory_rows = []
    write_json(run / 'status.json', {'status': 'running', 'source': video['source']})

    def inspect(indices, stage):
        for n, index in enumerate(indices, 1):
            if index in observations:
                continue
            previous = [r for i, r in observations.items() if i < index]
            previous = max(previous, key=lambda r: r['frame']) if previous else None
            context = {'previous_count': len(previous['people']) if previous else None}
            context_key = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()[:8]
            path = cache / f'{index:08}-{context_key}.json'
            if path.exists():
                row = json.loads(path.read_text('utf-8'))
            else:
                raw = detector.inspect(dict(frames[index], **context))
                data, error = parse_people(raw)
                row = dict(data, frame=index, seconds=times[index], raw=raw, parse_error=error)
                write_json(path, row)
            observations[index] = row
            status = str(len(row['people'])) if row['complete'] else 'неясно'
            if row.get('candidate_people') is not None:
                status = f"{len(row['people'])} прямых; спорных: {len(row.get('disputed_people', []))} (проверка отражений)"
            elif not row['complete']:
                review = row.get('review', {})
                status += f" (YOLO: {len(row['people'])}, Qwen: {review.get('qwen_count', 'нет ответа')})"
            print(f"[{stage}] {n}/{len(indices)} | {times[index]:.3f} с | людей: {status}", flush=True)
            if inventory_model is not None and stage == '1 FPS':
                from .inventory import inspect_inventory, save_inventory, display_frame_report
                print(f"[Предметы] {times[index]:.3f} с | поиск сейфов и вещей на столах...",flush=True)
                if inventory_variant=='legacy':
                    item_row=inspect_inventory(inventory_model,frames[index],Path(directory)/'inventory-cache')
                else:
                    from .inventory_runtime import inspect_with_memory
                    item_row=inspect_with_memory(inventory_model,detector,frames[index],directory,inventory_rows,inventory_variant)
                inventory_rows.append(item_row)
                save_inventory(inventory_rows,run)
                display_frame_report(item_row)
                if item_row.get('memory'):
                    m=item_row['memory']
                    print(f"[Память] объектов в истории: {m['remembered_count']}; видно сейчас: {m['observed_count']}; изменений: {len(m['events'])}",flush=True)

    try:
        inspect(sample_indices(times, 1), '1 FPS')
        coarse = list(observations.values())
        reconcile_reflections(coarse)
        _, triggers = track_people(coarse)
        # Furniture detector flicker is audited separately, not a reason to rescan all people frames.
        windows = refinement_windows(times, triggers)
        fine_indices = [i for i in sample_indices(times, 3) if any(lo <= i <= hi for lo, hi in windows)]
        inspect(fine_indices, '3 FPS')
        ordered = [observations[i] for i in sorted(observations)]
        reconcile_reflections(ordered)
        tracks, _ = track_people(ordered)
        scene = build_scene(ordered)
        region_reviews = []
        candidates = []
        for event in scene['events']:
            if event['to'] == 'visible':
                continue
            snapshot = next(s for s in scene['snapshots'] if s['frame'] == event['frame'])
            target = next(o for o in snapshot['objects'] if o['id'] == event['object_id'])
            candidates.append((event['frame'], target, 'object_disappeared'))
        # Configured zones are hypotheses, never inserted as observed objects.
        for zone in zones or []:
            for row in coarse[::5]:
                candidates.append((row['frame'], zone, 'configured_zone_check'))
        candidates.sort(key=lambda x: (x[0], x[1]['id']))
        if hasattr(detector, 'inspect_region'):
            for index, target, reason in candidates[:max_region_reviews]:
                region_dir = run / 'regions'
                region_dir.mkdir(exist_ok=True)
                cache_key = hashlib.sha256(json.dumps(target, sort_keys=True).encode()).hexdigest()[:12]
                path = region_dir / f'{index:08}-{cache_key}.json'
                if path.exists():
                    result = json.loads(path.read_text('utf-8'))
                else:
                    print(f"[ROI] {times[index]:.3f} с | {target['label']}", flush=True)
                    result = detector.inspect_region(frames[index], target, region_dir)
                    write_json(path, result)
                region_reviews.append({'seconds': times[index], 'frame': index, 'reason': reason, **result})
                # Only matching detector boxes update memory; Qwen interpretation remains audit evidence.
                if reason == 'object_disappeared':
                    from .tracking import similarity
                    for detection in result['detections']:
                        if detection['label'] == target['label'] and similarity(detection['bbox'], target['bbox']) >= .2:
                            observations[index].setdefault('objects', []).append(dict(detection, source='region'))
                            break
        scene = build_scene(ordered)
        scene['region_reviews'] = region_reviews
        scene['deferred_reviews'] = [{'frame': i, 'target': t, 'reason': reason}
                                     for i, t, reason in candidates[len(region_reviews):]]
        write_json(run / 'scene.json', scene)
        evidence = run / 'evidence'
        evidence.mkdir(exist_ok=True)
        for track in tracks:
            track.pop('last_detection', None)
            track.pop('missing', None)
            for number, visit in enumerate(track['visits'], 1):
                visit['evidence'] = {}
                for edge in ('in', 'out'):
                    index = visit[f'{edge}_frame']
                    if index is not None:
                        dest = evidence / f"{track['id']}-{number}-{edge}.png"
                        shutil.copyfile(frames[index].get('original_path', frames[index]['path']), dest)
                        visit['evidence'][edge] = str(dest)
        initial = ordered[0]
        report = {
            'status': 'done', 'source': video['source'], 'source_sha256': video['sha256'],
            'analysis_limit_seconds': video['analysis_limit_seconds'],
            'model': detector.metadata, 'initial_count': len(initial['people']) if initial['complete'] else None,
            'enhancement': video.get('enhancement'), 'scene_path': str(run / 'scene.json'),
            'inventory_path': str(run/'inventory.json') if inventory_rows else None,
            'inventory_frames': len(inventory_rows),
            'region_reviews': len(region_reviews), 'deferred_region_reviews': len(scene['deferred_reviews']),
            'qwen_calls': sum(r.get('review', {}).get('qwen_called', detector.metadata.get('backend') is None) for r in ordered)
                + sum('raw' in r for r in region_reviews)
                + sum('raw' in r.get('edge_review', {}) for r in region_reviews)
                + sum(r.get('qwen_requests',1) for r in inventory_rows),
            'elapsed_seconds': time.perf_counter() - started,
            'employee_count': sum(t['role'] == 'employee' for t in tracks),
            'client_count': sum(t['role'] == 'client' for t in tracks),
            'unknown_count': sum(t['role'] == 'unknown' for t in tracks), 'tracks': tracks,
            'coarse_frames': len(coarse), 'fine_grid_frames': len(fine_indices),
            'unique_images_analyzed': len(ordered),
            'incomplete_observations': sum(not r['complete'] for r in ordered),
            'parse_errors': sum(r['parse_error'] is not None for r in ordered),
            'refinement_windows_seconds': [[times[a], times[b]] for a, b in windows],
            'report_path': str(run / 'result.json'),
            'settings': {'coarse_fps': 1, 'fine_fps': 3, 'lookback_seconds': 1,
                         'window_seconds': 3, 'images_per_request': 1, 'missing_confirmation_observations': 2},
            'limitations': ['Roles are heuristics, not employment or customer identity.',
                'Tracking uses boxes and clothing, not faces. Crossings, occlusions and re-entry may split or swap tracks.',
                'Brief visits between 1 FPS samples may be missed. 3 FPS refinement is not native-frame precision.',
                'One frame cannot prove inactivity; only visible seated/arms-resting pose is checked.',
                'Two complete missing observations confirm departure; the reported exit is the first of those observations.'],
        }
        if actions_model is not None:
            from .action_pipeline import run_actions
            action_report=run_actions(actions_model,video,directory,detector)
            report['actions_path']=str(Path(action_report['output'])/'result.json')
            report['actions_review']=action_report['review_html']
            report['action_frames']=action_report['frames']
            report['elapsed_seconds']=time.perf_counter()-started
        write_json(run / 'observations.json', ordered)
        write_json(run / 'result.json', report)
        with (run / 'people_counts.csv').open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['Seconds', 'Direct people', 'Disputed figures', 'Excluded reflections', 'Complete'])
            for row in ordered:
                writer.writerow([row['seconds'], len(row['people']), len(row.get('disputed_people', [])),
                                 len(row.get('excluded_reflections', [])), row['complete']])
        with (run / 'objects.csv').open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['Object ID', 'Label', 'Seconds', 'Previous state', 'State', 'Reason'])
            for event in scene['events']:
                writer.writerow([event['object_id'], event['label'], event['seconds'], event['from'], event['to'], event['reason']])
        with (run / 'events.csv').open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['ID', 'Роль', 'Уже на первом кадре', 'Вход', 'Выход', 'Вход, с', 'Выход, с'])
            for track in tracks:
                for visit in track['visits']:
                    writer.writerow([track['id'], ROLES[track['role']], visit['already_present'],
                        visit['in_timecode'], visit['out_timecode'], visit['in_seconds'], visit['out_seconds']])
        write_json(run / 'status.json', {'status': 'done', 'report': report['report_path']})
        display(report)
        return report
    except BaseException as exc:
        write_json(run / 'status.json', {'status': 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed',
                                        'error': str(exc)})
        raise
