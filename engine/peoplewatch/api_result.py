"""Strict competition response adapter; no fabricated verdicts or evidence times."""
import base64
import io
import math
from pathlib import Path


class UnresolvedResult(ValueError):
    pass


def _seconds(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:
        raise ValueError('Invalid source timestamp')
    return float(value)


def evidence_index(rows,events=()):
    result={}
    def add(path,seconds):
        key=str(Path(path).resolve()); seconds=_seconds(seconds)
        if key in result and abs(result[key]-seconds)>1e-6:
            raise ValueError('One evidence file has contradictory timestamps')
        result[key]=seconds
    for row in rows:
        if row.get('source'): add(row['source'],row['seconds'])
    for event in events:
        for answer in (event.get('temporal_refinement') or {}).get('answers',{}).values():
            if answer.get('source'): add(answer['source'],answer['seconds'])
    return result


def make_result(job_id,report,rows,allowed_roots):
    if report.get('Status')!='completed' or report.get('pipeline_status')!='done':
        raise UnresolvedResult('analysis_incomplete')
    if report.get('source_partial') or report.get('fast_study'):
        raise UnresolvedResult('analysis_incomplete')
    if report.get('recording',{}).get('state')!='end_of_file':
        raise UnresolvedResult('source_completion_not_verified')
    verdict=report.get('Verdict')
    if verdict not in ('violation_suspected','no_violation_observed'):
        raise UnresolvedResult('insufficient_evidence')
    findings=report.get('violations')
    if not isinstance(findings,list) or bool(findings)!=(verdict=='violation_suspected'):
        raise ValueError('Contradictory verdict and violations')
    output=dict(job_id=job_id,status='done',verdict='violation' if findings else 'no_violation',violations=[])
    if report.get('assessment_mode')=='best_effort':
        if report.get('preliminary') is not True: raise ValueError('Best-effort result must disclose uncertainty')
        output.update(assessment_mode='best_effort',preliminary=True,
            decision_summary=report.get('decision_summary','Предварительный вывод; требуется проверка.'),
            unresolved_questions=len(report.get('review_issues',[]))+
                sum(bool(r.get('parse_error')) for r in report.get('visual_episode_reviews',[])))
    if not findings: return output
    roots=[Path(p).resolve(strict=True) for p in allowed_roots]
    if not roots: raise ValueError('Evidence roots required')
    index=evidence_index(rows,report.get('events',[]))
    from PIL import Image
    for finding in findings:
        if not isinstance(finding.get('type'),str) or not finding['type'].strip():
            raise ValueError('Missing violation type')
        if not isinstance(finding.get('description'),str) or not finding['description'].strip():
            raise ValueError('Missing description')
        moment=_seconds(finding.get('seconds'))
        evidence=[]
        paths=list(dict.fromkeys(finding.get('evidence_frames') or [finding.get('evidence_frame')]))
        if not paths or any(not p for p in paths): raise ValueError('No evidence frames')
        for source in paths:
            path=Path(source).resolve(strict=True)
            if not any(path.is_relative_to(root) for root in roots): raise ValueError('Evidence path outside job')
            if str(path) not in index: raise ValueError('Evidence timestamp was not recorded')
            evidence.append((index[str(path)],path))
        evidence.sort(key=lambda pair:pair[0])
        if len(evidence)>3:
            key=min(range(len(evidence)),key=lambda i:abs(evidence[i][0]-moment))
            picks={0,key,len(evidence)-1}
            if len(picks)<3: picks.add(len(evidence)//2)
            evidence=[evidence[i] for i in sorted(picks)]
        frames=[]
        for timestamp,path in evidence:
            with Image.open(path) as image:
                encoded=io.BytesIO(); image.convert('RGB').save(encoded,format='JPEG',quality=90)
            frames.append(dict(timestamp_sec=timestamp,image_base64=base64.b64encode(encoded.getvalue()).decode('ascii')))
        output['violations'].append(dict(type=finding['type'],timecode_sec=moment,
            description=finding['description'],evidence_frames=frames))
    return output
