from bisect import bisect_left, bisect_right


def sample_indices(times, fps=5):
    if fps <= 0:
        raise ValueError('FPS must be positive')
    if not times:
        return []
    selected, target = [], times[0]
    for i, t in enumerate(times):
        if t + 1e-9 >= target:
            selected.append(i)
            target = times[0] + (int((t - times[0]) * fps + 1e-8) + 1) / fps
    if selected[-1] != len(times) - 1:
        selected.append(len(times) - 1)
    return selected


def merge_windows(windows):
    merged = []
    for lo, hi in sorted(windows):
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
        else:
            merged.append((lo, hi))
    return merged


def window_around(times, lo, hi, padding):
    return (max(0, bisect_left(times, times[lo] - padding)),
            min(len(times) - 1, bisect_right(times, times[hi] + padding) - 1))


def initial_windows(times, observations, padding):
    if padding <= 0:
        raise ValueError('Padding must be positive')
    return merge_windows([window_around(times, r.frame, r.frame, padding)
                          for r in observations if r.state != 'absent'])


def expand_windows(times, windows, states, padding, confirmation):
    """Expand until both boundaries have enough native-frame absent evidence."""
    expanded = []
    for lo, hi in windows:
        left = range(lo, min(hi + 1, lo + confirmation))
        right = range(max(lo, hi - confirmation + 1), hi + 1)
        left_clear = len(left) == confirmation and all(states[i].state == 'absent' for i in left)
        right_clear = len(right) == confirmation and all(states[i].state == 'absent' for i in right)
        extended_lo, extended_hi = window_around(times, lo, hi, padding)
        if not left_clear and lo > 0:
            lo = min(lo - 1, extended_lo)
        if not right_clear and hi < len(times) - 1:
            hi = max(hi + 1, extended_hi)
        expanded.append((lo, hi))
    return merge_windows(expanded)
