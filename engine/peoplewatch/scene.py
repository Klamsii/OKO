"""Chronological object memory. A missed detection is never proof of removal."""
from peoplewatch.tracking import similarity


def covered_fraction(box, person):
    a, b = box, person
    area = max(0, min(a[2], b[2])-max(a[0], b[0])) * max(0, min(a[3], b[3])-max(a[1], b[1]))
    return area / ((a[2]-a[0])*(a[3]-a[1]))


def build_scene(rows):
    objects, events, snapshots = [], [], []
    previous_time = None
    for row in sorted(rows, key=lambda r: r['seconds']):
        used = set()
        for detection in row.get('objects', []):
            if detection['label'] == 'person':
                continue
            candidates = [(similarity(t['bbox'], detection['bbox']), i, t) for i, t in enumerate(objects)
                          if i not in used and t['label'] == detection['label']]
            score, index, track = max(candidates, default=(0, None, None), key=lambda v: v[0])
            if score < .2:
                track = {'id': f'O{len(objects)+1:03}', 'label': detection['label'],
                         'state': None, 'first_seen': row['seconds'], 'bbox': detection['bbox']}
                objects.append(track); index = len(objects)-1
            used.add(index)
            track.update(bbox=detection['bbox'], last_seen=row['seconds'])
        current = []
        for i, track in enumerate(objects):
            coverage = max([covered_fraction(track['bbox'], p['bbox']) for p in row['people']], default=0)
            state = 'visible' if i in used else ('likely_occluded' if coverage >= .5 else 'unknown')
            if track['state'] != state:
                events.append({'object_id': track['id'], 'label': track['label'], 'seconds': row['seconds'],
                               'frame': row['frame'], 'from': track['state'], 'to': state,
                               'transition_bounds_seconds': [previous_time, row['seconds']],
                               'person_box_coverage': coverage,
                               'reason': 'detected' if state == 'visible' else
                               'person_box_overlap_not_proof' if state == 'likely_occluded' else 'not_detected'})
            track['state'] = state
            current.append({**track, 'person_box_coverage': coverage})
        snapshots.append({'frame': row['frame'], 'seconds': row['seconds'], 'objects': current})
        previous_time = row['seconds']
    return {'objects': objects, 'events': events, 'snapshots': snapshots,
            'limitations': ['Box overlap suggests occlusion but does not prove its cause.',
                           'Hidden door state is unknown. No legal conclusions.',
                           'Object memory assumes a stationary camera; moving objects may receive new IDs.']}
