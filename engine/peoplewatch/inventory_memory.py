"""Causal inventory memory. History never manufactures a current detection."""
from copy import deepcopy
import json

VERSION = 'inventory-memory-v2'


def position_key(value):
    words = str(value).lower().replace('center', 'middle').split()
    return tuple(sorted(set(words)))


def observations(inv):
    for safe in inv.get('safes', []):
        yield dict(safe, category='safe', support='floor_or_pedestal', surface='room')
    for table in inv.get('tables', []):
        surface = 'table:' + str(position_key(table.get('position', 'unknown')))
        for item in table.get('items', []):
            yield dict(item, surface=surface)
    for item in inv.get('held', []):
        yield dict(item, surface='hands')


def advance(inv, previous=None):
    previous = previous or {}
    tracks = deepcopy(previous.get('objects', []))
    t = inv['seconds']
    if previous and t <= previous['seconds']:
        raise ValueError('Memory must advance chronologically')
    events, used = [], set()
    bounds = [previous.get('seconds'), t]
    for obj in observations(inv):
        category = obj.get('category', 'other')
        exact = [x for x in tracks if x['id'] not in used and x['category'] == category
                 and x['surface'] == obj['surface'] and position_key(x.get('position')) == position_key(obj.get('position'))]
        # Conservative one-to-one matching; positional matches are hypotheses, not re-identification.
        matches = exact
        if not matches and category != 'other':
            same = [x for x in tracks if x['id'] not in used and x['category'] == category
                    and t - x['last_seen'] <= 3]
            current_same = [x for x in observations(inv) if x.get('category') == category]
            if len(same) == len(current_same) == 1:
                matches = same
        matched = len(matches) == 1
        if matched:
            track = matches[0]
            before = {k: track.get(k) for k in ('state', 'support', 'surface', 'certainty')}
        else:
            track = dict(id=f'I{len(tracks)+1:04}', first_seen=t, history=[])
            tracks.append(track)
            before = {}
        used.add(track['id'])
        track.update({k: obj.get(k) for k in ('label','category','support','surface','certainty','position')})
        track.update(state='observed', last_seen=t, identity='positional_hypothesis')
        track['history'].append(dict(seconds=t, frame=inv.get('frame'), **obj))
        reason = None
        if not matched:
            reason = 'first_observation'
        elif before['state'] != 'observed':
            reason = 'observed_again'
        elif before['support'] != track['support'] or before['surface'] != track['surface']:
            reason = 'support_changed'
        elif before['certainty'] == 'possible' and track['certainty'] == 'clear':
            reason = 'identity_more_clear'
        if reason:
            events.append(dict(object_id=track['id'], event=reason, seconds=t, bounds_seconds=bounds,
                               previous=before, current={k: track[k] for k in ('label','support','surface','certainty')}))
    for track in tracks:
        if track['id'] not in used:
            if track['state'] == 'observed':
                events.append(dict(object_id=track['id'], event='not_observed', seconds=t,
                                   bounds_seconds=bounds, reason='absence_is_not_proof_of_removal'))
            track['state'] = 'not_observed'
    return dict(version=VERSION, seconds=t, objects=tracks, events=events,
                observed_count=len(used), remembered_count=len(tracks))


def context(previous_rows):
    if not previous_rows:
        return ''
    last = previous_rows[-1].get('inventory', previous_rows[-1])
    memory = last.get('memory', {})
    now = last.get('seconds', memory.get('seconds', 0))
    recent = [x for x in memory.get('objects', [])
              if x.get('state') == 'observed' or now - x.get('last_seen', -1e9) <= 2]
    recent.sort(key=lambda x: (x.get('state') == 'observed', x.get('last_seen', -1)), reverse=True)
    # Keep only distinct, recent visual hypotheses in the model prompt. The
    # complete event/track history remains in saved results for audit.
    objects, seen = [], set()
    for item in recent:
        key = (item.get('category'), item.get('support'), position_key(item.get('position')),
               str(item.get('label', '')).lower().strip())
        if key in seen:
            continue
        seen.add(key)
        objects.append({
            'label': str(item.get('label', ''))[:48],
            'category': item.get('category'),
            'support': {'on_table':'table','on_device':'device','in_hand':'hand'}.get(
                item.get('support'), item.get('support')),
            'position': item.get('position'),
            'state': item.get('state'),
            'last_seen': item.get('last_seen'),
        })
        if len(objects) == 12:
            break
    return ('\nPrevious scene observations at ' + str(last.get('seconds')) + ' seconds (may be wrong): '
            + json.dumps(objects, ensure_ascii=False, separators=(',', ':')) + '\nCurrent image is later. Independently check it. '
            'Recheck unclear identities and small objects from history. Include newly visible objects immediately. '
            'Do NOT copy a remembered object into the current visible inventory unless it is visible now. '
            'Do not infer disappearance, theft, or movement from missing detections. Do not assume object counts stay fixed.')
