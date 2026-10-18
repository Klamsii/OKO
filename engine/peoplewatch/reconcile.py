"""Replay reflection decisions on stored real model responses without loading models."""
import argparse
import csv
import json
from pathlib import Path
from .reflections import reconcile_reflections
from .tracking import track_people
from cashwatch.video import write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('run', type=Path)
    args = parser.parse_args()
    rows = json.loads((args.run / 'observations.json').read_text('utf-8'))
    reconcile_reflections(rows)
    tracks, _ = track_people(rows)
    root = args.run / 'reflection-audit-v2'; root.mkdir(exist_ok=True)
    write_json(root / 'audit.json', {'mode': 'replay of stored model responses; no new inference',
               'source_run': str(args.run), 'observations': rows, 'tracks': tracks})
    with (root / 'people_counts.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f); writer.writerow(['Seconds','Direct','Disputed','Excluded reflections','Complete'])
        for row in rows:
            values = [row['seconds'],len(row['people']),len(row.get('disputed_people',[])),
                      len(row.get('excluded_reflections',[])),row['complete']]
            writer.writerow(values); print(values)
    print(root / 'audit.json')


if __name__ == '__main__':
    main()
