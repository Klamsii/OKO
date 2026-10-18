"""Cross-field consistency, never a claim that model evidence is ground truth."""


def counter_state(row):
    if not row or row.get('parse_error'): return 'unknown'
    state=row.get('counter','unknown')
    visible_cash=any(x.get('kind')=='cash' and x.get('location')=='counter' for x in row.get('objects',[]))
    if state=='loaded' and not visible_cash: return 'unknown'
    if state=='empty' and visible_cash: return 'unknown'
    return state
