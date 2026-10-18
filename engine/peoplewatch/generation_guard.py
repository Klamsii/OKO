"""Stop only irrevocably invalid normalized boxes; do not repair model answers."""
import re

BOX=re.compile(r'"bbox"\s*:\s*\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]')


def invalid_box(text):
    # The original table parser rejects >24 rows BEFORE duplicate removal.
    # Restrict this rule to the table-only response, not multi-table or safe lists.
    if re.match(r'^\s*\{\s*"complete"\s*:\s*(?:true|false)\s*,\s*"items"\s*:\s*\[',text):
        if len(re.findall(r'"label"\s*:',text))>24:
            return 'More than 24 table items; original parser would reject this reply'
    for match in BOX.finditer(text):
        a,b,c,d=map(float,match.groups())
        if not (0<=a<c<=1000 and 0<=b<d<=1000): return match.group(0)
    return None


def install_box_guard(qwen,log_path):
    from transformers import StoppingCriteria,StoppingCriteriaList
    from pathlib import Path
    import json,time
    generate=qwen.model.generate
    class Guard(StoppingCriteria):
        def __init__(self,start): self.start=start; self.reason=None
        def __call__(self,input_ids,scores,**kwargs):
            if (input_ids.shape[-1]-self.start)%8: return False
            text=qwen.processor.tokenizer.decode(input_ids[0,self.start:],skip_special_tokens=True)
            self.reason=invalid_box(text)
            return self.reason is not None
    def guarded_generate(*args,**kwargs):
        guard=Guard(kwargs['input_ids'].shape[-1])
        kwargs['stopping_criteria']=StoppingCriteriaList([guard])
        result=generate(*args,**kwargs)
        if guard.reason:
            with Path(log_path).open('a',encoding='utf-8') as stream:
                stream.write(json.dumps(dict(time=time.time(),reason=guard.reason),ensure_ascii=False)+'\n')
            print('Stopped invalid normalized coordinates: '+guard.reason,flush=True)
        return result
    qwen.model.generate=guarded_generate
