import math
import re
from functools import lru_cache

from cashwatch.events import timecode


def similarity(a, b):
    x = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    y = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    intersection = x * y
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection
    return intersection / union if union else 0


def match_cost(track, person, seconds):
    old = track['last_detection']
    a, b = old['bbox'], person['bbox']
    overlap = similarity(a, b)
    distance = math.hypot((a[0] + a[2] - b[0] - b[2]) / 2000,
                          (a[1] + a[3] - b[1] - b[3]) / 2000)
    words = lambda s: set(re.findall(r'[a-z]+', s.lower())) - {'a', 'and', 'person', 'wearing', 'unknown'}
    left, right = words(old['clothing']), words(person['clothing'])
    appearance = len(left & right) / len(left | right) if left | right else 0
    if seconds - track['last_seconds'] > 3:
        return None
    if overlap < .05 and (distance > .3 or appearance < .2):
        return None
    if not track['active'] and appearance < .35:
        return None
    return .5 * (1 - overlap) + distance + .3 * (1 - appearance)


def assign(tracks, people, seconds):
    costs = [[match_cost(t, p, seconds) for p in people] for t in tracks]
    # Small scenes use a global assignment, avoiding greedy swaps at crossings.
    if len(people) <= 10 and len(tracks) <= 12:
        @lru_cache(None)
        def solve(i, used):
            if i == len(tracks):
                return 0, ()
            tail, pairs = solve(i + 1, used)
            best = (1.15 + tail, pairs)
            for j, cost in enumerate(costs[i]):
                if cost is None or used & (1 << j) or cost >= 1.15:
                    continue
                tail, pairs = solve(i + 1, used | (1 << j))
                candidate = (cost + tail, ((i, j),) + pairs)
                if candidate[0] < best[0]:
                    best = candidate
            return best
        return list(solve(0, 0)[1])
    pairs, used_tracks, used_people = [], set(), set()
    for cost, i, j in sorted((c, i, j) for i, row in enumerate(costs) for j, c in enumerate(row)
                             if c is not None and c < 1.15):
        if i not in used_tracks and j not in used_people:
            pairs.append((i, j)); used_tracks.add(i); used_people.add(j)
    return pairs


def track_people(rows, missing_confirm=2):
    tracks, triggers, previous = [], set(), None
    previous_complete = None
    for row in sorted(rows, key=lambda r: r['frame']):
        seconds, people = row['seconds'], row['people']
        if not row['complete']:
            triggers.add(row['frame'])
        if previous is not None and len(people) != len(previous['people']):
            triggers.add(row['frame'])
        candidates = [t for t in tracks if t['active'] or seconds - t['last_seconds'] <= 3]
        pairs = assign(candidates, people, seconds)
        matched = set()
        seen_ids = set()

        def start_visit(track):
            track['active'] = True
            track['missing'] = []
            track['visits'].append({
                'in_seconds': seconds, 'in_timecode': timecode(seconds), 'in_frame': row['frame'],
                'already_present': row['frame'] == 0,
                'entry_bounds_seconds': [previous_complete['seconds'] if previous_complete else None, seconds],
                'out_seconds': None, 'out_timecode': None, 'out_frame': None,
                'exit_bounds_seconds': None, 'exit_candidate_seconds': None,
            })
            triggers.add(row['frame'])

        for i, j in pairs:
            track = candidates[i]
            if not track['active']:
                start_visit(track)
            track['missing'] = []
            track['visits'][-1]['exit_candidate_seconds'] = None
            detection = dict(people[j])
            if detection['clothing'] == 'unknown':
                detection['clothing'] = track['last_detection']['clothing']
            track.update(last_detection=detection, last_seconds=seconds, last_frame=row['frame'])
            matched.add(j); seen_ids.add(track['id'])
        for j, person in enumerate(people):
            if j in matched:
                continue
            first_frame = row['frame'] == 0
            employee = (first_frame and row['complete'] and len(people) == 1
                        and person['posture'] == 'seated' and person['arms'] == 'resting')
            role = 'employee' if employee else ('unknown' if first_frame else 'client')
            track = {'id': f'P{len(tracks) + 1:03}', 'role': role, 'active': True,
                     'last_detection': person, 'last_seconds': seconds, 'last_frame': row['frame'],
                     'initial_clothing': person['clothing'], 'visits': [], 'missing': []}
            start_visit(track)
            tracks.append(track); seen_ids.add(track['id'])
        for track in tracks:
            if not track['active'] or track['id'] in seen_ids:
                continue
            if not row['complete']:
                track['missing'] = []
                track['visits'][-1]['exit_candidate_seconds'] = None
                continue
            track['missing'].append((row['frame'], seconds))
            first_frame, first_seconds = track['missing'][0]
            track['visits'][-1]['exit_candidate_seconds'] = first_seconds
            triggers.add(first_frame)
            if len(track['missing']) >= missing_confirm:
                track['visits'][-1].update(out_seconds=first_seconds, out_timecode=timecode(first_seconds),
                    out_frame=first_frame, exit_bounds_seconds=[track['last_seconds'], first_seconds],
                    exit_candidate_seconds=None)
                track['active'] = False
        previous = row
        if row['complete']:
            previous_complete = row
    return tracks, sorted(triggers)
