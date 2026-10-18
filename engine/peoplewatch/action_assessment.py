"""Shared assessment for live runs and re-evaluation of saved observations."""
import json
from pathlib import Path

from cashwatch.video import write_json
from .compliance import assess,timecode
from .sequence_review import review,supported


def assess_sequence(qwen,rows,events,partial=False,sequence_path=None,visual_candidates=None,decision_mode='strict'):
    if decision_mode not in ('strict','best_effort'): raise ValueError('Invalid decision mode')
    assessment=assess(rows,events,partial=partial)
    strict_rule_verdict=assessment['Verdict']
    path=Path(sequence_path) if sequence_path is not None else None
    if path is not None and decision_mode!='strict': path=path.with_name(path.stem+'-'+decision_mode+path.suffix)
    if path is not None and path.exists():
        sequence=json.loads(path.read_text('utf-8'))
    else:
        print('[Последовательность] Проверка действий и правил...',flush=True)
        sequence=review(qwen,rows,partial,decision_mode)
        if path is not None: write_json(path,sequence)
    unsupported=0
    candidates_source=list(sequence)
    if visual_candidates: candidates_source.append(dict(violations=visual_candidates))
    for result in candidates_source:
        if result.get('parse_error'): continue
        for candidate in result.get('violations',[]):
            verified=supported(candidate,rows)
            from .best_effort import provisional_support,DESCRIPTIONS
            provisional=decision_mode=='best_effort' and provisional_support(candidate,rows)
            if not verified and not provisional:
                unsupported+=1
                continue
            cited=[rows[i] for i in sorted(set(candidate['observation_ids']))]
            key=candidate.get('key_observation_id')
            moment=rows[key] if type(key) is int and key in candidate['observation_ids'] else cited[-1]
            t=moment['seconds']
            if any(v['type']==candidate['type'] and abs(v['seconds']-t)<1 for v in assessment['violations']): continue
            assessment['violations'].append(dict(type=candidate['type'],seconds=t,timecode=timecode(t),
                description=candidate['description'] if verified else DESCRIPTIONS[candidate['type']],evidence_frame=moment['source'],
                evidence_frames=[r['source'] for r in cited],time_interval_seconds=[cited[0]['seconds'],cited[-1]['seconds']],
                certainty='possible',requires_review=True,evidence_level='sequence_supported' if verified else 'preliminary_visual_anchor',rule_id='sequence_review_candidate',
                rule_source='Свод_правил_кассовая_дисциплина.md'))
    from .review_issues import scoped_issues
    issues=scoped_issues(sequence,rows)
    errors=sum(bool(r.get('parse_error')) for r in sequence)
    uncertain=any(i['blocking'] and i['code']!='sequence_review_parse_error' for i in issues)
    if assessment['violations']: assessment['Verdict']='violation_suspected'
    elif errors or uncertain or unsupported: assessment['Verdict']='insufficient_evidence'
    if errors:
        assessment['abstention_reasons'].append(dict(code='sequence_review_parse_error',count=errors,
            issues=[i for i in issues if i['code']=='sequence_review_parse_error']))
    if uncertain:
        assessment['abstention_reasons'].append(dict(code='sequence_review_uncertain',
            count=sum(i['blocking'] and i['code']!='sequence_review_parse_error' for i in issues),
            issues=[i for i in issues if i['blocking'] and i['code']!='sequence_review_parse_error']))
    if unsupported:
        assessment['abstention_reasons'].append(dict(code='unsupported_sequence_claim',count=unsupported))
    if errors: assessment['limitations'].append('Sequence reasoning failed to return a valid structured answer.')
    if uncertain: assessment['limitations'].append('Essential unanswered questions are scoped to their cited observations in review_issues.')
    if unsupported: assessment['limitations'].append('Unsupported sequence claims were retained in the raw review and excluded from violations.')
    assessment['sequence_review_errors']=errors
    assessment['unsupported_sequence_claims']=unsupported
    from .review_issues import observation_issues
    assessment['review_issues']=issues+observation_issues(assessment['abstention_reasons'],rows)
    from .issue_impact import classify,possible_violations
    assessment['review_issues']=classify(assessment['review_issues'],rows)
    assessment['possible_violations']=possible_violations(assessment['review_issues'],rows)
    if decision_mode=='best_effort':
        for finding in assessment['possible_violations']:
            if not any(v['type'] in (finding['type'],finding['underlying_type']) and
                       abs(v['seconds']-finding['seconds'])<1 for v in assessment['violations']):
                assessment['violations'].append(finding)
        from .best_effort import finish
        assessment['_strict_rule_verdict']=strict_rule_verdict
        finish(assessment,rows,partial)
    else: assessment.update(assessment_mode='strict',preliminary=False)
    from .issue_impact import moment_assessments
    assessment['moment_assessments']=moment_assessments(rows,assessment['review_issues'],
        assessment['violations']+assessment['possible_violations'])
    return assessment
