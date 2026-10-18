"""Auditable full 1 FPS inventory experiments; no labels or fixed scene geometry in inference."""
import argparse
import hashlib
import json
import shutil
import time
from pathlib import Path

from cashwatch.planning import sample_indices
from cashwatch.video import write_json
from .video import prepare
from .enhance import Settings, prepare_images


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--round',required=True)
    p.add_argument('--variant',default='baseline')
    p.add_argument('--prompt-lookup',action='store_true')
    p.add_argument('--root',type=Path,default=Path('D:/DATATHON/inventory_study'))
    p.add_argument('--source',type=Path,default=Path('C:/Users/олд/Downloads/open/open/videos/X-1.mov'))
    args=p.parse_args()
    out=args.root/args.round
    out.mkdir(parents=True,exist_ok=True)
    snapshot=out/'code'; snapshot.mkdir(exist_ok=True)
    for source in Path(__file__).parent.glob('*.py'):
        if not (snapshot/source.name).exists(): shutil.copy2(source,snapshot/source.name)
    directory,video=prepare(args.source,Path('D:/DATATHON/peoplewatch_results'))
    prepare_images(video,directory,Settings(mode='auto'))
    frames=[video['frames'][i] for i in sample_indices([f['seconds'] for f in video['frames']],1)]
    write_json(out/'manifest.json',dict(variant=args.variant,source=video['source'],frames=frames,
        started=time.strftime('%Y-%m-%dT%H:%M:%S'),sha256=hashlib.sha256(args.source.read_bytes()).hexdigest()))
    from .detector import QwenPeopleDetector,parse_people
    from .hybrid import HybridDetector
    from .inventory import inspect_inventory,display_frame_report
    print('Loading Qwen for '+args.round,flush=True)
    qwen=QwenPeopleDetector(Path('D:/DATATHON/Qwen3-VL-8B-Instruct'))
    write_json(out/'model.json',qwen.metadata)
    if args.variant=='baseline':
        from .generation_guard import install_box_guard
        install_box_guard(qwen,out/'invalid-generation-stops.jsonl')
    if args.prompt_lookup:
        from .inventory import PROMPT
        expected=json.loads((args.root/'00_baseline'/'frame-01.json').read_text('utf-8'))['inventory']['raw']
        tick=time.perf_counter()
        qwen.model.generation_config.prompt_lookup_num_tokens=8
        qwen.model.generation_config.max_matching_ngram_size=4
        probe=dict(enabled=False)
        try:
            raw=qwen.inspect_prompt(frames[0]['original_path'],PROMPT,max_new_tokens=1536,max_edge=848,max_seconds=90)
            probe.update(raw=raw,exact_match=raw==expected,enabled=raw==expected)
        except Exception as exc:
            probe['error']=repr(exc)
        probe['elapsed_seconds']=time.perf_counter()-tick
        write_json(out/'prompt-lookup-probe.json',probe)
        print('Prompt lookup probe: '+str({k:v for k,v in probe.items() if k!='raw'}),flush=True)
        if not probe['enabled']:
            qwen.model.generation_config.prompt_lookup_num_tokens=None
            qwen.model.generation_config.max_matching_ngram_size=None
    detector=HybridDetector(Path('D:/DATATHON/models/yolo11n.pt'),qwen)
    rows=[]; started=time.perf_counter()
    for number,frame in enumerate(frames,1):
        target=out/f"frame-{number:02}.json"
        if target.exists():
            rows.append(json.loads(target.read_text('utf-8'))); continue
        tick=time.perf_counter()
        if args.variant=='baseline':
            people_path=out/f'people-{number:02}.json'
            if people_path.exists(): people=json.loads(people_path.read_text('utf-8'))
            else:
                raw=detector.inspect(dict(frame,previous_count=len(rows[-1]['people']['people']) if rows else None))
                people,error=parse_people(raw)
                people.update(raw=raw,parse_error=error,frame=frame['index'],seconds=frame['seconds'])
                write_json(people_path,people)
            inventory=inspect_inventory(qwen,frame,out/'inventory-cache')
            row=dict(people=people,inventory=inventory)
        else:
            from .inventory_experiments import inspect_frame
            row=inspect_frame(qwen,detector,frame,out,args.variant,rows)
        row.update(seconds=frame['seconds'],frame=frame['index'],source=frame['original_path'],
                   elapsed_seconds=time.perf_counter()-tick)
        write_json(target,row); rows.append(row)
        write_json(out/'results.json',dict(variant=args.variant,complete=len(rows)==len(frames),
            frames=rows,elapsed_seconds=time.perf_counter()-started))
        print(f"ROUND {args.round} {number}/{len(frames)} time {row['elapsed_seconds']:.1f}s",flush=True)
        display_frame_report(row['inventory'])
    write_json(out/'results.json',dict(variant=args.variant,complete=True,frames=rows,
                                     elapsed_seconds=time.perf_counter()-started))
    print('COMPLETE '+args.round,flush=True)


if __name__=='__main__': main()
