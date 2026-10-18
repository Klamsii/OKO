"""Adaptive action analysis: 1 FPS overview plus motion/event-driven 3 FPS review."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import time

from cashwatch.planning import sample_indices
from cashwatch.video import write_json
from .actions import inspect,event_timeline,RUSSIAN,VERSION,PROMPT
from .video import source_is_partial
from .compliance import recording_context,apply_recording_context


def motion_scores(video):
    """Cheap image differences inspect ALL available 3 FPS frames, independent of semantic detections."""
    import numpy as np
    from PIL import Image,ImageFilter
    frames=video['frames']; times=[x['seconds'] for x in frames]
    indices=sample_indices(times,3); scores={}; previous=None
    for i in indices:
        path=frames[i].get('original_path') or frames[i]['path']
        with Image.open(path) as image:
            arr=np.asarray(image.convert('L').resize((96,160)).filter(ImageFilter.GaussianBlur(.5)),dtype=np.float32)
        if previous is None: score=0.
        else:
            delta=arr-previous
            delta-=np.median(delta)  # Reduce uniform lighting flicker, not local hand motion.
            changed=(np.abs(delta)>12).reshape(10,16,6,16).mean(axis=(1,3))
            score=float(changed.max())
        scores[i]=score; previous=arr
    return scores


def uncertainty_reasons(row):
    """Visual questions worth a 3 FPS revisit, without changing the raw answer."""
    reasons=[]
    if row.get('parse_error') or (row.get('crop_review') or {}).get('parse_error'):
        reasons.append('unreadable_observation')
    objects=row.get('objects') or []
    contacts=row.get('contacts') or []
    if any(x.get('certainty')=='possible' for x in objects):
        reasons.append('possible_object')
    if any(x.get('certainty')=='possible' and x.get('relation')!='other' for x in contacts):
        reasons.append('possible_hand_contact')
    if row.get('counter')=='unknown' and (any(x.get('kind')=='cash' for x in objects) or
                                         any(x.get('relation')=='at_counter' for x in contacts)):
        reasons.append('counter_state_unknown_during_cash')
    overview=row.get('overview_observation') or {}
    if overview and any(x.get('kind')=='cash' for x in objects) and not any(
            x.get('kind')=='cash' for x in overview.get('objects',[])):
        reasons.append('overview_crop_disagree_on_cash')
    return reasons


def review_indices(video,coarse_rows,scores,threshold=.5):
    times=[x['seconds'] for x in video['frames']]
    # Local motion catches brief changes even when no 1 FPS image showed the object.
    triggers=[times[i] for i,score in scores.items() if score>=threshold]
    triggers.extend(e['seconds'] for e in event_timeline(coarse_rows) if not e['type'].startswith('safe_initial'))
    # A stable-looking hand or a missed small note need not produce motion or
    # an event. Revisit uncertain observations on both sides at 3 FPS.
    uncertain=[r['seconds'] for r in coarse_rows if uncertainty_reasons(r)]
    return [i for i in sample_indices(times,3)
            if any(t-1.0<=times[i]<=t+1.0 for t in triggers) or
               any(t-1.5<=times[i]<=t+1.5 for t in uncertain)]


def budget_samples(indices,scores,limit):
    """Development-only selection distributed across the clip; never labelled exhaustive."""
    if limit is None or len(indices)<=limit: return indices
    if limit<2: raise ValueError('At least two samples required')
    selected={indices[0],indices[-1]}
    inner=indices[1:-1]
    for n in range(limit-2):
        group=inner[n*len(inner)//(limit-2):(n+1)*len(inner)//(limit-2)]
        if group: selected.add(max(group,key=lambda i:scores.get(i,0)))
    return sorted(selected)


def run_actions(qwen,video,directory,detector=None,refine=True,fast_study=False,
                pose_cropper=None,decision_mode='best_effort'):
    from .inventory_experiments import automatic_work_crop
    modules=('actions.py','action_pipeline.py','counter_motion.py','counter_evidence.py','boundary_oracle.py',
             'temporal_refinement.py','compliance.py','sequence_review.py','sequence_context.py',
             'evidence_support.py','hand_focus.py','action_assessment.py','video.py',
             'episode_review.py','review_issues.py','best_effort.py','issue_impact.py')
    code_digest=hashlib.sha256(b''.join((Path(__file__).parent/name).read_bytes() for name in modules)).hexdigest()
    signature=hashlib.sha256((VERSION+code_digest+qwen.signature+str(refine)+str(fast_study)+decision_mode+
                              str(getattr(pose_cropper,'signature',None))).encode()).hexdigest()[:12]
    out=Path(directory)/('actions-'+signature); out.mkdir(parents=True,exist_ok=True)
    import shutil
    snapshot=out/'code'; snapshot.mkdir(exist_ok=True)
    for module in Path(__file__).parent.glob('*.py'):
        if not (snapshot/module.name).exists(): shutil.copy2(module,snapshot/module.name)
    write_json(out/'model.json',qwen.metadata)
    rows={}; frames=video['frames']; times=[f['seconds'] for f in frames]
    source_partial=source_is_partial(video)
    partial=fast_study or source_partial
    recording=recording_context(video,partial=partial)
    limit=video.get('analysis_limit_seconds',video.get('max_seconds'))
    shared_cache=Path(directory)/'action_observation_cache'; shared_cache.mkdir(exist_ok=True)
    vision_code=hashlib.sha256((Path(__file__).parent/'actions.py').read_bytes()+
        (Path(__file__).parent/'inventory_experiments.py').read_bytes()).hexdigest()
    started=time.perf_counter()
    write_json(out/'status.json',dict(status='running',source=video['source']))
    events=[]; assessment=None; processing_stage='motion_sampling'
    stage_started=started; stage_times={}
    def enter_stage(name):
        nonlocal processing_stage,stage_started
        now=time.perf_counter()
        stage_times[processing_stage]=stage_times.get(processing_stage,0)+now-stage_started
        processing_stage=name; stage_started=now
    def analyze(indices,stage):
        for n,i in enumerate(indices,1):
            if i in rows: continue
            frame=frames[i]; source=frame.get('original_path') or frame['path']
            prior=max((r for j,r in rows.items() if j<i),key=lambda r:r['seconds'],default=None)
            # Coarse context is chronological; fine-pass caches include their actual preceding observation.
            context_key=hashlib.sha256(json.dumps(prior,sort_keys=True).encode()).hexdigest()[:10]
            target=out/f'{i:08}-{stage}-{context_key}.json'
            prompt_context={k:prior.get(k) for k in ('seconds','safes','objects','contacts','counter',
                'visitor_present','desk_person_present','parse_error')} if prior else None
            shared_key=hashlib.sha256(json.dumps(dict(model=qwen.signature,vision=vision_code,prompt=PROMPT,
                observation_policy='original-and-zoom-one-request-v3',stage=stage,
                detector=getattr(detector,'signature',None),pose=getattr(pose_cropper,'signature',None),
                image=hashlib.sha256(Path(source).read_bytes()).hexdigest(),
                seconds=times[i],context=prompt_context),sort_keys=True).encode()).hexdigest()
            shared=shared_cache/(shared_key+'.json')
            if target.exists(): row=json.loads(target.read_text('utf-8'))
            elif shared.exists():
                row=json.loads(shared.read_text('utf-8'))
                # The same verified pixels may live under a different run root.
                # Evidence must point to this run's portable frame copy.
                row.update(frame=i,stage=stage,source=str(source),cache_hit=True)
                write_json(target,row)
            else:
                tick=time.perf_counter()
                # Small-object review uses an automatically detected work area. Full safe view is retained.
                # An empty overview may be a missed small object, not evidence of absence.
                hand=(pose_cropper.crop(source,out/'hand-crops'/f'{i:08}.png')
                      if pose_cropper is not None and stage=='3fps' else None)
                crop=(automatic_work_crop(detector,dict(frame,original_path=source),out)
                      if detector is not None and hand is None else None)
                zoom_path=hand['path'] if hand else (crop['zoom_path'] if crop else None)
                joint=zoom_path is not None and hasattr(qwen,'inspect_sequence')
                row=(inspect(qwen,source,times[i],prior,zoom_path=zoom_path) if joint else
                     inspect(qwen,source,times[i],prior))
                row.update(frame=i,stage=stage,elapsed_seconds=time.perf_counter()-tick)
                if crop and not joint:
                    if crop:
                        zoom=inspect(qwen,crop['zoom_path'],times[i],None,focused=True)
                        row['crop_review']=zoom
                        if not zoom['parse_error']:
                            row['overview_observation']={k:row[k] for k in ('objects','contacts','counter')}
                            # A crop cannot erase objects/hands outside its view. Retain both views and their conflicts.
                            for key,identity in (('objects',('kind','location')),('contacts',('actor','relation','object'))):
                                existing={tuple(x[k] for k in identity) for x in row[key]}
                                row[key]=row[key]+[dict(x,view='crop') for x in zoom[key]
                                    if tuple(x[k] for k in identity) not in existing]
                            if row['counter']=='unknown': row['counter']=zoom['counter']
                if hand and not joint:
                    hand_zoom=inspect(qwen,hand['path'],times[i],None,focused=True)
                    row['hand_review']=dict(hand_zoom,crop_bounds_pixels=hand['bounds_pixels'],
                                            original_frame=source)
                    if not hand_zoom['parse_error']:
                        for key,identity in (('objects',('kind','location')),
                                             ('contacts',('actor','relation','object'))):
                            existing={tuple(x[k] for k in identity) for x in row[key]}
                            row[key]=row[key]+[dict(x,view='hands') for x in hand_zoom[key]
                                if tuple(x[k] for k in identity) not in existing]
                        if row['counter']=='unknown': row['counter']=hand_zoom['counter']
                row['elapsed_seconds']=time.perf_counter()-tick
                write_json(target,row)
                write_json(shared,row)
            rows[i]=row
            ordered=[rows[j] for j in sorted(rows)]
            events=event_timeline(ordered)
            write_json(out/'observations.json',ordered)
            write_json(out/'events.json',events)
            summary='; '.join(RUSSIAN.get(e['type'],e['type']) for e in events if e['seconds']==times[i])
            print(f"[Действия {stage}] {n}/{len(indices)} | {times[i]:.3f} с | {summary or 'новое событие не выявлено'}",flush=True)
    try:
        if recording['state']=='read_error':
            raise RuntimeError('Prepared source records a reading/decoding failure: '+recording['error'])
        scores=motion_scores(video)
        write_json(out/'motion.json',scores)
        coarse_candidates=sample_indices(times,1)
        coarse_selected=budget_samples(coarse_candidates,scores,20 if fast_study else None)
        enter_stage('coarse_observation')
        analyze(coarse_selected,'1fps')
        coarse=[rows[i] for i in sorted(rows)]
        fine_candidates=review_indices(video,coarse,scores) if refine else []
        uncertainty_triggers=[dict(seconds=r['seconds'],reasons=uncertainty_reasons(r))
                              for r in coarse if uncertainty_reasons(r)]
        fine=budget_samples([i for i in fine_candidates if i not in rows],scores,12 if fast_study else None)
        write_json(out/'sampling.json',dict(coarse_frames=len(coarse),coarse_candidates=len(coarse_candidates),
            fine_candidates=len(fine_candidates),fine_selected=len(fine),fast_study=fast_study,
            uncertainty_triggers=uncertainty_triggers,
            pose_hand_review=pose_cropper is not None,
            partial=partial,source_partial=source_partial,analysis_limit_seconds=limit,
            recording=recording,
            source_duration_seconds=video.get('source_duration_seconds'),
            motion_threshold=.5,images_per_request=1,fine_fps=3,
            uncertainty_window_seconds_before=1.5,uncertainty_window_seconds_after=1.5,
            coverage=('DEVELOPMENT SAMPLE ONLY' if fast_study else
                ('PARTIAL SOURCE; ' if source_partial else '')+
                ('1 FPS throughout prepared interval; 3 FPS around motion/events' if refine else
                 '1 FPS throughout prepared interval; temporal refinement disabled'))))
        enter_stage('fine_observation')
        analyze(fine,'3fps')
        ordered=[rows[i] for i in sorted(rows)]; events=event_timeline(ordered)
        from .evidence_support import cash_presence_support
        cash_support=cash_presence_support(ordered)
        write_json(out/'cash_presence_support.json',cash_support)
        enter_stage('counter_motion')
        from .counter_motion import locate_in_video,measure,candidates
        counter_path=out/'counter_motion.json'
        if counter_path.exists(): counter_audit=json.loads(counter_path.read_text('utf-8'))
        else:
            location=locate_in_video(qwen,frames)
            counter_audit=dict(location=location,signal=measure(video['source'],location['bbox'])
                if video.get('source_kind')!='image' else [])
            write_json(counter_path,counter_audit)
        events.extend(candidates(ordered,counter_audit['signal'],device_confirmed=not counter_audit['location'].get('candidate_only',False)))
        events.sort(key=lambda e:e['seconds'])
        enter_stage('visual_episode_review')
        from .episode_review import review as review_episodes,reviewed_events
        write_json(out/'events_before_visual_review.json',events)
        reviewed,visual_reviews,visual_candidates=review_episodes(qwen,ordered,events,out/'episode-review',
                                                                 limit=2 if fast_study else 4)
        events=reviewed_events(reviewed,events)
        raw_cash_support=cash_support
        cash_support=cash_presence_support(reviewed)
        write_json(out/'cash_presence_support_reviewed.json',cash_support)
        from .action_assessment import assess_sequence
        enter_stage('sequence_assessment')
        assessment=assess_sequence(qwen,reviewed,events,partial=partial,sequence_path=out/'sequence_review.json',
                                   visual_candidates=visual_candidates,decision_mode=decision_mode)
        apply_recording_context(assessment,recording)
        from .boundary_oracle import refine_event,PREDICATES
        # Source-frame precision is expensive. Refine only a transition whose
        # meaning was checked in actual chronological images; never every cash
        # flicker in the overview. Keep all other candidate bounds in the report.
        topic={'safe_opened':'safe_opening','safe_closed':'safe_closing','object_at_pocket':'pocket_transfer'}
        critical=[]
        for event in events:
            if event['type'] not in PREDICATES or event['bounds_seconds'][0] is None: continue
            matched=[r for r in visual_reviews if r['question']==topic.get(event['type']) and
                not r.get('parse_error') and r.get('answer',{}).get('state')=='yes' and
                min(r['seconds'])<=event['seconds']<=max(r['seconds'])]
            if matched:
                event['semantic_verification']='visual_sequence_model_supported'
                critical.append(event)
            else: event['refinement_skipped']='temporal_meaning_not_confirmed'
        critical.sort(key=lambda e:(e['type']!='object_at_pocket',e['seconds']))
        selected=critical[:(1 if fast_study else 3)] if refine else []
        enter_stage('temporal_refinement')
        for number,event in enumerate(selected,1):
            print(f"[Точное время] {number}/{len(selected)} | {event['type']} | {event['bounds_seconds']}",flush=True)
            detail=refine_event(qwen,video['source'],event,out/f"precise-{number:03}",budget=4 if fast_study else 8)
            event['temporal_refinement']=detail
            event['timing_status']='model_estimated'
            event['semantic_verification']='visual_sequence_model_supported'
            if detail and detail.get('transitions'):
                desired=event['type'] not in ('cash_not_visible','safe_closed')
                relevant=[x for x in detail['transitions'] if x['to_state']==desired]
                if relevant:
                    event['original_bounds_seconds']=event['bounds_seconds']
                    event['refined_transitions']=relevant
                    # Multiple transitions remain separate; do not discard a return after disappearance.
                    event['bounds_seconds']=relevant[0]['bounds_seconds']
                    event['seconds']=relevant[0]['seconds']
                    event['evidence_frames']=relevant[0]['evidence_frames']
        events.sort(key=lambda e:e['seconds'])
        enter_stage('report')
        write_json(out/'events.json',events)
        with (out/'events.csv').open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.writer(stream); writer.writerow(['Время, с','Событие','Начало интервала, с','Конец интервала, с','Статус','Кадры'])
            for e in events:
                writer.writerow([e['seconds'],RUSSIAN.get(e['type'],e['type']),*e['bounds_seconds'],e['status'],' | '.join(e['evidence_frames'])])
        report=dict(pipeline_status='done',source=video['source'],source_sha256=video['sha256'],version=VERSION,frames=len(rows),events=events,fast_study=fast_study,
            cash_presence_support=cash_support,
            raw_cash_presence_support=raw_cash_support,
            visual_episode_reviews=visual_reviews,
            native_refinement_budget=dict(max_events=1 if fast_study else 3,frames_per_event=4 if fast_study else 8,
                                          selected_events=len(selected)),
            source_partial=source_partial,analysis_limit_seconds=limit,
            source_duration_seconds=video.get('source_duration_seconds'),analysis_bounds_seconds=[times[0],times[-1]],
            output=str(out),elapsed_seconds=time.perf_counter()-started,limitations=[
                'Model observations and event candidates require visual review; no legal verdict.',
                'A loaded counter is not proof that counting started. No counting_started event is fabricated.',
                '3 FPS may miss subsecond movements; interval bounds are not native-frame precision.',
                'Desk/visitor sides are visual roles. Bundle and safe identity across frames is provisional.',
                'Coarse observations retain their original causal context; later review never rewrites their raw answers.'])
        if source_partial:
            report['limitations'].append('Only a limited source interval was analyzed; later actions and violations remain unobserved.')
        assessment['limitations']=report['limitations']+assessment['limitations']
        report.update(assessment)
        from .violation_reporting import describe_violations
        report['violations']=describe_violations(report['violations'])
        report['possible_violations']=describe_violations(report.get('possible_violations',[]))
        report['elapsed_seconds']=time.perf_counter()-started
        report['stage_seconds']=dict(stage_times,report=time.perf_counter()-stage_started)
        report['model_signature']=qwen.signature
        report['processing_time_seconds']=report['elapsed_seconds']
        bounds=report['analysis_bounds_seconds']
        report['analyzed_duration_seconds']=max(0.0,bounds[1]-bounds[0])
        write_json(out/'verdict.json',{k:report[k] for k in ('Status','Verdict','violations','transactions',
                   'abstention_reasons','review_issues',
                   'unresolved_transactions','episode_states','recording','scope','limitations',
                   'assessment_mode','preliminary','decision_summary','strict_rule_verdict',
                   'working_assumptions','possible_violations','moment_assessments') if k in report})
        from .action_report import write_review
        report['review_html']=write_review(report)
        write_json(out/'result.json',report); write_json(out/'status.json',dict(status='done'))
        print(f"Status: {report['Status']} | Verdict: {report['Verdict']} | violations: {len(report['violations'])}",flush=True)
        for violation in report['violations']:
            print(f"  {violation['timecode']} | {violation['type']} | {violation['description']}",flush=True)
        print('Просмотр событий: '+report['review_html'],flush=True)
        return report
    except BaseException as exc:
        failed_video=dict(video)
        if exc.__class__.__module__.startswith('av.'):
            failed_video['decode_error']=repr(exc)
        failure_context=recording_context(failed_video,partial=True,error=repr(exc))
        from .compliance import assess
        # Persist already supported observations even if a later processing stage
        # fails. A technical failure must not become a successful empty verdict.
        ordered=[rows[i] for i in sorted(rows)]
        if assessment is None: assessment=assess(ordered,event_timeline(ordered),partial=True)
        apply_recording_context(assessment,failure_context)
        failed=dict(assessment,pipeline_status='failed',source=video['source'],output=str(out),
            error=repr(exc),failure_stage=processing_stage,frames=len(ordered),events=events)
        write_json(out/'result.json',failed)
        write_json(out/'status.json',dict(status='failed',error=repr(exc),stage=processing_stage,recording=failure_context))
        raise


def main():
    p=argparse.ArgumentParser()
    p.add_argument('videos',nargs='+',type=Path)
    p.add_argument('--coarse-only',action='store_true')
    p.add_argument('--decision-mode',choices=['strict','best_effort'],default='best_effort')
    p.add_argument('--fast-study',action='store_true',help='Development sample only: max 20 overview + 12 detailed frames per clip')
    args=p.parse_args()
    Path('D:/DATATHON/action_study').mkdir(parents=True,exist_ok=True)
    from .detector import QwenPeopleDetector
    from .hybrid import HybridDetector
    from .video import prepare
    qwen=QwenPeopleDetector(Path('D:/DATATHON/Qwen3-VL-8B-Instruct'))
    qwen.model.generation_config.prompt_lookup_num_tokens=8
    qwen.model.generation_config.max_matching_ngram_size=4
    detector=HybridDetector(Path('D:/DATATHON/models/yolo11n.pt'),qwen)
    results=[]
    for path in args.videos:
        directory,video=prepare(path,Path('D:/DATATHON/peoplewatch_results'))
        results.append(run_actions(qwen,video,directory,detector,not args.coarse_only,args.fast_study,decision_mode=args.decision_mode))
        write_json(Path('D:/DATATHON/action_study')/'run-results.json',results)


if __name__=='__main__': main()
