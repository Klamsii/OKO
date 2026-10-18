"""Link uncertainty to observable rule-relevant patterns, not merely to cash presence."""
from .review_issues import NON_BLOCKING
from .compliance import timecode

TECHNICAL={'sequence_review_parse_error','unreadable_observation','source_gap',
           'partial_analysis','source_or_processing_incomplete','no_observations'}
CASHLIKE={'cash','paper_unknown'}


def patterns(rows,ids):
    selected=[rows[i] for i in ids]
    result={}
    for i,row in zip(ids,selected):
        money=[o for o in row.get('objects',[]) if o.get('kind') in CASHLIKE]
        contacts=[c for c in row.get('contacts',[]) if c.get('object') in CASHLIKE]
        if any(o.get('location')=='pocket' for o in money) or any(c.get('relation')=='at_pocket' for c in contacts):
            result.setdefault('cash_at_personal_pocket',[]).append(i)
    splits=[i for i in ids if any(c.get('object') in CASHLIKE and c.get('relation')=='separating_bundle'
                                   for c in rows[i].get('contacts',[]))]
    for i in ids:
        locations={o.get('location') for o in rows[i].get('objects',[]) if o.get('kind') in CASHLIKE}
        if any(j<i for j in splits) and 'visitor_hand' in locations and locations & {'desk_hand','table','counter'}:
            result['cash_split_unresolved']=sorted(set(splits+[i]))
    # A presence change alone is a hypothesis, not a proven departure/new customer.
    for previous,current in zip(ids,ids[1:]):
        a,b=rows[previous],rows[current]
        if (a.get('desk_person_present')=='yes' and b.get('desk_person_present')=='no' and
            a.get('visitor_present')==b.get('visitor_present')=='yes'):
            result.setdefault('employee_left_during_service',[]).extend([previous,current])
    cash_table=[i for i in ids if any(o.get('kind') in CASHLIKE and o.get('location')=='table' for o in rows[i].get('objects',[]))]
    if len(cash_table)>=2:
        phase=0; visit_ids=[]
        for i in ids:
            presence=rows[i].get('visitor_present')
            if (phase in (0,2) and presence=='yes') or (phase==1 and presence=='no'):
                visit_ids.append(i); phase+=1
                if phase==3: break
        if phase==3 and cash_table[0]<=visit_ids[1]<=cash_table[-1]:
            result['cash_left_unsecured_between_visits']=sorted(set(cash_table+visit_ids))
    return {kind:sorted(set(values)) for kind,values in result.items()}


def classify(issues,rows):
    result=[]
    for source in issues:
        issue=dict(source)
        ids=sorted({i for i in issue.get('observation_ids',[]) if type(i) is int and 0<=i<len(rows)})
        code=issue['code']; relevant=patterns(rows,ids)
        if code in NON_BLOCKING:
            impact='informational'; reason='Эта деталь не является обязательным признаком проверяемого нарушения.'; relevant={}
        elif code in TECHNICAL:
            impact='technical'; reason='Техническая неполнота влияет на качество анализа, но сама не является нарушением.'; relevant={}
        else:
            if code in ('cash_identity','cash_origin','cash_disposition','hidden_transfer','counting_or_handling'):
                relevant={k:v for k,v in relevant.items() if k!='employee_left_during_service'}
            if code=='service_completion':
                relevant={k:v for k,v in relevant.items() if k in ('employee_left_during_service','cash_left_unsecured_between_visits')}
            if code=='counting_or_handling': relevant={}  # None of the currently checked four patterns requires counting.
            impact='possible_violation' if relevant else 'review_only'
            reason=('Ответ может изменить оценку наблюдаемого подозрительного эпизода.' if relevant else
                    'В указанных наблюдениях нет связанного подозрительного действия. Одной неопределённости недостаточно для нарушения.')
        issue.update(impact=impact,affects_verdict=impact in ('possible_violation','technical'),
            impact_reason=reason,related_patterns=relevant)
        result.append(issue)
    return result


def possible_violations(issues,rows):
    grouped={}
    for issue in issues:
        if issue.get('impact')!='possible_violation': continue
        for kind,anchors in issue['related_patterns'].items():
            key=(kind,tuple(anchors))
            record=grouped.setdefault(key,dict(kind=kind,anchors=anchors,codes=set(),questions=[]))
            record['codes'].add(issue['code']); record['questions'].append(issue['description'])
    from .best_effort import DESCRIPTIONS
    findings=[]
    for record in grouped.values():
        anchors=record['anchors']; first,last=anchors[0],anchors[-1]
        # Include available immediate context, not invented sub-frame boundaries.
        ids=sorted(set(anchors+[max(0,first-1),min(len(rows)-1,last+1)]))
        moment=rows[last]
        findings.append(dict(type='possible_'+record['kind'],underlying_type=record['kind'],
            status='possible_violation',certainty='unconfirmed',requires_review=True,
            evidence_level='unresolved_visual_question',affects_verdict=True,
            seconds=moment['seconds'],timecode=timecode(moment['seconds']),
            time_interval_seconds=[rows[ids[0]]['seconds'],rows[ids[-1]]['seconds']],
            timing_status='review_interval_not_confirmed_event',
            evidence_frame=moment['source'],evidence_frames=[rows[i]['source'] for i in ids],
            observation_ids=ids,rule_id='unresolved_visual_question',
            description='Возможное нарушение — требует проверки. '+DESCRIPTIONS[record['kind']]+
                ' Неясно: '+'; '.join(dict.fromkeys(record['questions']))+
                ' Таймкоды обозначают участок проверки, а не доказанные границы нарушения.',
            unresolved_codes=sorted(record['codes'])))
    return findings


def moment_assessments(rows,issues,findings):
    """Summarize sampled moments; an irrelevant detail never hides another finding."""
    result=[]
    for i,row in enumerate(rows):
        t=row['seconds']
        active=[q for q in issues if i in q.get('observation_ids',[])]
        matches=[]
        for finding in findings:
            bounds=finding.get('time_interval_seconds') or [finding['seconds'],finding['seconds']]
            if bounds[0] is not None and bounds[1] is not None and bounds[0]<=t<=bounds[1]: matches.append(finding)
        technical=bool(row.get('parse_error')) or any(q.get('impact')=='technical' for q in active)
        if matches:
            verdict='possible_violation'; label='Возможное нарушение'
            reason='В этом участке есть подозрительный признак или существенный вопрос по нему; требуется проверка.'
        elif technical:
            verdict='analysis_error'; label='Момент не оценён из-за технической ошибки'
            reason='Техническая ошибка не является нарушением и не позволяет объявить этот момент чистым.'
        else:
            verdict='no_violation_observed'; label='Нарушение в этом моменте не выявлено'
            reason='Оставшиеся вопросы не связаны с наблюдаемым подозрительным действием.' if active else 'На проверенных кадрах признаки поддерживаемых нарушений не найдены.'
        types=sorted({f['type'] for f in matches}); codes=sorted({q['code'] for q in active})
        signature=(verdict,types,codes)
        if (result and result[-1]['_signature']==signature and t-result[-1]['end_seconds']<=1.1):
            result[-1]['end_seconds']=t; result[-1]['observation_ids'].append(i)
        else:
            result.append(dict(start_seconds=t,end_seconds=t,verdict=verdict,label=label,
                affects_verdict=verdict!='no_violation_observed',reason=reason,preliminary=True,
                sampling_scope='evaluated_frames_only',related_types=types,question_codes=codes,
                observation_ids=[i],_signature=signature))
    for item in result: item.pop('_signature')
    return result
