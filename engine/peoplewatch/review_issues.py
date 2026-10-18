"""Local review questions. Unknown details do not invalidate unrelated evidence."""

# Only details which cannot establish any supported rule may be non-blocking.
# The VLM cannot exempt cash identity, custody, a hidden hand, or a source gap.
NON_BLOCKING = {'banknote_denomination', 'exact_banknote_count', 'device_brand',
                'person_name', 'object_colour'}


def parse_questions(data, allowed_ids):
    if 'questions' not in data:
        return None  # Older saved output retains conservative legacy behavior.
    questions=data['questions']
    if not isinstance(questions,list): raise ValueError('Invalid review questions')
    result=[]
    for question in questions:
        if not isinstance(question,dict): raise ValueError('Invalid review question')
        code=question.get('code'); ids=question.get('observation_ids')
        description=question.get('description')
        if not isinstance(code,str) or not code.strip(): raise ValueError('Missing question code')
        if not isinstance(description,str) or not description.strip(): raise ValueError('Missing question description')
        if not isinstance(ids,list) or not ids or any(type(i) is not int or i not in allowed_ids for i in ids):
            raise ValueError('Invalid question evidence references')
        result.append(dict(code=code,description=description,observation_ids=sorted(set(ids)),
                           blocking=code not in NON_BLOCKING,scope='episode'))
    return result


def scoped_issues(sequence, rows):
    issues=[]
    for chunk_index,result in enumerate(sequence):
        ids=(result.get('context') or {}).get('current_observation_ids',[])
        ids=[i for i in ids if type(i) is int and 0<=i<len(rows)]
        if result.get('parse_error'):
            questions=[dict(code='sequence_review_parse_error',description=result['parse_error'],
                            observation_ids=ids,blocking=True)]
        elif result.get('verdict')=='insufficient_evidence':
            questions=result.get('questions') or [dict(code='sequence_review_uncertain',
                description=result.get('unresolved') or 'Unspecified essential question',
                observation_ids=ids,blocking=True)]
        else:
            questions=result.get('questions') or []
        for question in questions:
            cited=question['observation_ids']
            times=[rows[i]['seconds'] for i in cited if 0<=i<len(rows)]
            issue=dict(question,chunk_index=chunk_index,scope='episode',
                       bounds_seconds=[min(times),max(times)] if times else [None,None])
            issues.append(issue)
    # The same referenced uncertainty can appear in overlapping memory excerpts.
    unique={}
    for issue in issues:
        key=(issue['code'],tuple(issue['observation_ids']))
        unique.setdefault(key,issue)
    return list(unique.values())


def observation_issues(reasons,rows):
    """Expose deterministic missing prerequisites alongside the VLM questions."""
    result=[]
    labels={'cash_identity':'Является ли видимый бумажный предмет банкнотами?',
            'cash_origin':'Откуда поступила наблюдаемая группа денег?',
            'counting_or_handling':'Какое действие выполнено с наблюдаемыми деньгами?',
            'cash_disposition':'Куда передана или помещена оставшаяся группа денег?',
            'service_completion':'Видно ли завершение обслуживания по этой операции?'}
    for reason in reasons:
        if reason['code'] not in ('unresolved_paper_identity','unresolved_cash_episode'): continue
        if reason.get('seconds'):
            ids=[i for i,r in enumerate(rows) if r['seconds'] in reason['seconds']]
        else:
            a,b=reason.get('start_seconds'),reason.get('end_seconds')
            ids=[i for i,r in enumerate(rows) if a is not None and b is not None and a<=r['seconds']<=b]
        if not ids: continue
        for code in reason.get('missing',[]):
            result.append(dict(code=code,description=labels.get(code,code),observation_ids=ids,
                blocking=True,scope='episode',source='observation_prerequisite',
                bounds_seconds=[rows[ids[0]]['seconds'],rows[ids[-1]]['seconds']]))
    return result
