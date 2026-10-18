from dataclasses import dataclass
from typing import Literal

State = Literal['present', 'absent', 'uncertain']


@dataclass(frozen=True)
class Observation:
    frame: int
    seconds: float
    state: State
    stage: str = 'fine'


def timecode(seconds):
    if seconds is None:
        return None
    milliseconds = round(seconds * 1000)
    hours, rest = divmod(milliseconds, 3600000)
    minutes, rest = divmod(rest, 60000)
    secs, ms = divmod(rest, 1000)
    return f'{hours:02}:{minutes:02}:{secs:02}.{ms:03}'


def episodes(observations, min_present=2, min_absent=2):
    """Confirm with consecutive source frames; uncertainty never means absent."""
    if min_present < 1 or min_absent < 1:
        raise ValueError('Confirmation counts must be positive')
    rows = sorted(observations, key=lambda row: row.frame)
    if len({r.frame for r in rows}) != len(rows):
        raise ValueError('Duplicate frame observations')
    if any(r.state not in ('present', 'absent', 'uncertain') for r in rows):
        raise ValueError('Invalid visibility state')
    found, rejected = [], []
    pending, absent_run = [], []
    active = None
    last_absent = last_present = None

    def reject_pending():
        if pending:
            rejected.append({'first_frame': pending[0].frame,
                             'first_seconds': pending[0].seconds,
                             'frames': len(pending), 'reason': 'not_confirmed'})
            pending.clear()

    for row in rows:
        if row.state == 'present':
            absent_run.clear()
            last_present = row
            if active is not None:
                active['last_visible_frame'] = row.frame
                active['last_visible_seconds'] = row.seconds
                continue
            if pending and row.frame != pending[-1].frame + 1:
                reject_pending()
            pending.append(row)
            if len(pending) < min_present:
                continue
            first = pending[0]
            active = {
                'episode': len(found) + 1,
                'in_seconds': first.seconds, 'in_timecode': timecode(first.seconds),
                'first_visible_frame': first.frame,
                'last_visible_frame': row.frame, 'last_visible_seconds': row.seconds,
                'entry_bounds_seconds': [last_absent.seconds if last_absent else None, first.seconds],
                'entry_adjacent_frames': bool(last_absent and last_absent.frame + 1 == first.frame),
                'already_visible_at_start': first.frame == 0,
                'out_seconds': None, 'out_timecode': None, 'first_absent_frame': None,
                'exit_bounds_seconds': None, 'exit_adjacent_frames': False,
                'open_end': True, 'uncertain_frames': 0, 'absent_observations_inside': 0,
            }
            pending.clear()
        elif row.state == 'uncertain':
            reject_pending()
            absent_run.clear()
            if active is not None:
                active['uncertain_frames'] += 1
        else:
            reject_pending()
            last_absent = row
            if active is None:
                continue
            active['absent_observations_inside'] += 1
            if absent_run and row.frame != absent_run[-1].frame + 1:
                absent_run.clear()
            absent_run.append(row)
            if len(absent_run) < min_absent:
                continue
            first = absent_run[0]
            active.update(out_seconds=first.seconds, out_timecode=timecode(first.seconds),
                          first_absent_frame=first.frame,
                          exit_bounds_seconds=[last_present.seconds, first.seconds],
                          exit_adjacent_frames=last_present.frame + 1 == first.frame,
                          open_end=False)
            active['absent_observations_inside'] -= len(absent_run)
            found.append(active)
            active = None
            absent_run.clear()
    reject_pending()
    if active is not None:
        found.append(active)
    return {
        'episodes': found,
        'episode_count': len(found),
        'appearance_count': sum(e['entry_bounds_seconds'][0] is not None for e in found),
        'disappearance_count': sum(e['out_seconds'] is not None for e in found),
        'already_visible_at_start_count': sum(e['already_visible_at_start'] for e in found),
        'unconfirmed_candidates': rejected,
    }
