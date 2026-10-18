"""Bounded chronological excerpts with selected earlier evidence, no label input."""
import json
from .counter_evidence import counter_state
from .evidence_support import cash_presence_support


def requests(rows,prompt,partial,token_count=None,max_tokens=2200,max_rows=16):
    count=token_count or (lambda text:(len(text)+2)//3)
    trace=[]
    support=cash_presence_support(rows)
    time_ids={row['seconds']:i for i,row in enumerate(rows)}
    for i,row in enumerate(rows):
        trace.append(dict(id=i,t=row['seconds'],people=[row.get('desk_person_present','unknown'),row.get('visitor_present','unknown')],
            objects=[[x.get(k) for k in ('kind','location','certainty')] for x in row.get('objects',[])],
            cash_support=[[x['location'],x['tier']] for x in support[i]],
            contacts=[[x.get(k) for k in ('actor','relation','object','certainty')] for x in row.get('contacts',[])],
            counter=counter_state(row),safes=[[x.get('position','')[:80],x.get('state')] for x in row.get('safes',[])],
            error=bool(row.get('parse_error') or row.get('crop_review',{}).get('parse_error'))))
        # A later visual review is additional model evidence, not another copy
        # of the original guess. Carry its actual references into reasoning.
        from .episode_review import CHECKS
        facts=[]
        for kind,fact in (row.get('episode_evidence') or {}).items():
            if kind not in CHECKS or not isinstance(fact,dict): continue
            times=fact.get('observation_seconds') or []
            if (fact.get('confirmed') is not True or fact.get('visual_review') is not True or
                    len(set(times))<3 or any(t not in time_ids for t in times) or
                    any(fact.get(check) is not True for check in CHECKS[kind])): continue
            facts.append(dict(type=kind,ids=[time_ids[t] for t in times],
                evidence=fact.get('evidence','')[:240],destination=fact.get('destination'),
                source='visual_sequence_model_review_not_ground_truth'))
        if facts: trace[-1]['temporal_facts']=facts
    header=prompt+'\nSampling is partial: '+str(partial)+'''
Compact row fields: id=global observation ID; t=seconds; people=[desk_person,visitor];
objects entries=[kind,location,certainty]; contacts entries=[actor,relation,object,certainty];
safes entries=[position,state]. cash_support=[location,tier] rates visible cash presence only;
moderate means repeated nearby possible observations, never proof of counting,
receipt, custody, identity, or a rule violation.
temporal_facts are additional visual sequence reviews with referenced IDs. Consider
them when a single-frame observation was ambiguous; they remain model evidence.
This is a bounded excerpt with selected earlier reference observations.
Time gaps are UNKNOWN, not proof that an object disappeared or an operation finished. TRACE:
'''
    result=[]; start=0
    while start<len(trace):
        references=set(range(max(0,start-3),start))
        prior=range(max(0,start-3))
        predicates=(lambda r:any(x.get('kind')=='cash' and x.get('location')=='desk_hand' for x in r.get('objects',[])),
                    lambda r:r.get('visitor_present')=='no',
                    lambda r:any(x.get('relation')=='separating_bundle' for x in r.get('contacts',[])),
                    lambda r:bool(r.get('episode_evidence')))
        for predicate in predicates:
            hits=[i for i in prior if predicate(rows[i])]
            if hits: references.add(hits[-1])
        def with_fact_references(ids):
            return sorted(set(ids)|{ref for i in ids for fact in trace[i].get('temporal_facts',[]) for ref in fact['ids']})
        def text_for(ids):
            ids=with_fact_references(ids)
            selected=[]
            for i in ids:
                item=dict(trace[i])
                # Additional reference rows can themselves carry other reviews.
                # Do not expose their unseen IDs or recursively grow the prompt.
                if 'temporal_facts' in item:
                    item['temporal_facts']=[f for f in item['temporal_facts'] if set(f['ids'])<=set(ids)]
                selected.append(item)
            return header+json.dumps(selected,ensure_ascii=False,separators=(',',':'))
        while references and count(text_for(sorted(references|{start})))>max_tokens:
            references.remove(min(references))
        end=start; chosen=None
        while end<len(trace) and end-start<max_rows:
            ids=sorted(references|set(range(start,end+1))); text=text_for(ids)
            if count(text)>max_tokens: break
            chosen=dict(ids=with_fact_references(ids),current_ids=list(range(start,end+1)),text=text,tokens=count(text)); end+=1
        if chosen is None: raise ValueError('A single observation exceeds the sequence context budget')
        result.append(chosen); start=end
    return result
