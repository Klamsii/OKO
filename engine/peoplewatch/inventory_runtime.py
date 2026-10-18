"""Production adapter for tested inventory passes, with auditable causal memory."""
import hashlib
import json
from pathlib import Path
from cashwatch.video import write_json
from .inventory_experiments import inspect_frame,PROMPT_V1,SMALL_PROMPT,VERIFY_PROMPT
from .inventory_memory import advance,VERSION


def inspect_with_memory(qwen,detector,frame,directory,previous,variant='memory'):
    previous_memory=previous[-1].get('memory') if previous else None
    key=hashlib.sha256(json.dumps(dict(model=qwen.signature,variant=variant,memory=previous_memory,
        seconds=frame['seconds'],frame=frame['index'],
        version=VERSION,prompts=[PROMPT_V1,SMALL_PROMPT,VERIFY_PROMPT],source=hashlib.sha256(
            Path(frame['original_path']).read_bytes()).hexdigest()),sort_keys=True).encode()).hexdigest()
    out=Path(directory)/('inventory-'+variant); out.mkdir(parents=True,exist_ok=True)
    path=out/(key+'.json')
    if path.exists(): return json.loads(path.read_text('utf-8'))
    row=inspect_frame(qwen,detector,frame,out,variant,previous)['inventory']
    if 'memory' not in row: row['memory']=advance(row,previous_memory)
    write_json(path,row)
    return row
