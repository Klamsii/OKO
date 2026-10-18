"""Project-rule candidates from observed sequences. Never reads organizer labels."""
RULES_SOURCE='Свод_правил_кассовая_дисциплина.md'
from .counter_evidence import counter_state


def recording_context(video=None, partial=False, error=None):
    """Decoder/program facts only. An image/model answer cannot establish EOF."""
    from .video import source_is_partial
    video=video or {}
    decode_error=video.get('decode_error') or video.get('read_error')
    if decode_error: state='read_error'
    elif error: state='processing_error'
    elif partial or source_is_partial(video): state='partial_analysis'
    elif video.get('source_kind')=='image': state='still_image'
    elif video.get('source_complete') is True and video.get('analysis_truncated') is False:
        state='end_of_file'
    else: state='end_not_recorded'
    return dict(state=state,source_complete=video.get('source_complete'),
        analysis_truncated=video.get('analysis_truncated'),
        error=str(decode_error or error) if decode_error or error else None,
        operation_completion_inferred=False)


def reviewed_fact(row,name,seen,**required):
    """Consume explicit sequence-review evidence, never synthesize it from no/yes.

    Optional episode_evidence is intentionally absent in legacy vision answers.
    Each confirmed fact cites at least two already observed times, including the
    current frame, and records the specific visible prerequisites. These remain
    review claims, not ground truth or face-based identification.
    """
    facts=row.get('episode_evidence') or {}
    if not isinstance(facts,dict): return None
    fact=facts.get(name,{})
    if not isinstance(fact,dict) or fact.get('confirmed') is not True: return None
    if not isinstance(fact.get('evidence'),str) or not fact['evidence'].strip(): return None
    if any(fact.get(key) is not value for key,value in required.items()): return None
    times=fact.get('observation_seconds',[])
    if not isinstance(times,list) or any(not isinstance(t,(int,float)) or isinstance(t,bool) for t in times): return None
    times=sorted(set(times)); known={r['seconds']:r for r in seen}
    if len(times)<2 or times[-1]!=row['seconds'] or any(t not in known for t in times): return None
    if any(known[t].get('parse_error') or known[t].get('crop_review',{}).get('parse_error') for t in times): return None
    return dict(evidence=fact['evidence'],bounds_seconds=[times[0],times[-1]],
                evidence_frames=[known[t]['source'] for t in times],observation_seconds=times)


def apply_recording_context(assessment,context):
    """Keep evidence and observation limits independent of technical completion."""
    assessment['recording']=context
    state=context['state']
    if state in ('read_error','processing_error'):
        assessment['Status']='failed'
    elif state=='partial_analysis': assessment['Status']='partial'
    if state in ('read_error','processing_error','partial_analysis') and not assessment.get('violations'):
        assessment['Verdict']='insufficient_evidence'
        reasons=assessment.setdefault('abstention_reasons',[])
        reason=dict(code='source_or_processing_incomplete',detail=state)
        if reason not in reasons: reasons.append(reason)
    for transaction in assessment.get('transactions',[]):
        transaction['recording_state']=state
        transaction['observation_incomplete']=not bool(transaction.get('disposition'))
        transaction['operation_completion_inferred_from_eof']=False
    if state!='end_of_file': return assessment
    # EOF cannot create an offence or settle custody. The existing internal
    # uncertainty verdict remains separate from an external API mapping.
    assessment['limitations'].append('The file ended; this does not establish completion of service or a missing required final action.')
    return assessment


def timecode(seconds):
    ms=round(seconds*1000); hours,ms=divmod(ms,3600000); minutes,ms=divmod(ms,60000); secs,ms=divmod(ms,1000)
    return f'{hours:02}:{minutes:02}:{secs:02}.{ms:03}'


def assess(rows,events,partial=False,recording=None):
    violations=[]; transactions=[]; current=None; unsecured_absence=None; previous=None; seen=[]
    episode_states=[]
    def add(kind,t,description,evidence,rule,interval=None,certainty='possible'):
        violations.append(dict(type=kind,timecode=timecode(t),seconds=t,description=description,
            evidence_frame=evidence[-1] if evidence else None,evidence_frames=evidence,
            time_interval_seconds=interval or [t,t],certainty=certainty,rule_id=rule,
            rule_source=RULES_SOURCE,requires_review=True))
    for row in sorted(rows,key=lambda r:r['seconds']):
        seen.append(row)
        if row.get('parse_error'):
            episode_states.append(dict(seconds=row['seconds'],customer='unknown',service='completion_unknown'))
            unsecured_absence=None
            previous=row; continue
        departure=reviewed_fact(row,'customer_departure',seen,same_person=True,movement_away=True,adjacent_confirmation=True)
        completion=reviewed_fact(row,'service_completion',seen,visible_completion=True)
        new_customer=reviewed_fact(row,'different_customer',seen,different_person=True,new_service_started=True)
        continuity=reviewed_fact(row,'cash_group_continuity',seen,same_group=True,continuously_visible=True,origin_received=True)
        presence=row.get('visitor_present')
        customer='present' if presence=='yes' else ('temporarily_not_visible' if presence=='no' else 'unknown')
        if departure: customer='departure_visible'
        money=[x for x in row['objects'] if x['kind']=='cash']
        locations={x['location'] for x in money}
        clear_table_cash=any(x.get('location')=='table' and x.get('certainty')=='clear' for x in money)
        old={x['location'] for x in previous.get('objects',[]) if x['kind']=='cash'} if previous else set()
        receipt='desk_hand' in locations and ('visitor_hand' in old or any(
            c['relation']=='handover_contact' and c['object']=='cash' for c in row['contacts']))
        custody=locations & {'desk_hand','counter','table','pocket','unknown'}
        if current is not None and current.get('disposition') and custody:
            # A later visible group starts a new hypothesis; never reopen a
            # completed group and overwrite its receipt/completion evidence.
            current=None
        if current is None and (receipt or custody):
            current=dict(episode_id=f'cash-{len(transactions)+1:04}',start=row['seconds'],evidence=[row['source']],
                         state='received_hypothesis' if receipt else 'origin_unobserved',
                         counted=False,disposition=None,receipt_seconds=row['seconds'] if receipt else None,
                         cash_identity=('confirmed_presence' if any(x.get('certainty')!='possible' for x in money)
                                        else 'possible_presence'),counter_loaded=False,
                         customer_state=customer,service_state='ongoing' if receipt else 'completion_unknown')
            transactions.append(current)
            unsecured_absence=None
        elif receipt and current and current['state']=='origin_unobserved':
            current['state']='received_hypothesis'; current['receipt_seconds']=row['seconds']
            current['evidence'].append(row['source'])
        if current:
            current['last_observed_seconds']=row['seconds']
            if any(x.get('certainty')!='possible' for x in money): current['cash_identity']='confirmed_presence'
            current['customer_state']=customer
            if departure: current['departure_evidence']=departure
            if completion:
                current['service_state']='visible_completion'
                current['completion_evidence']=completion
            elif receipt: current['service_state']='ongoing'
            elif not current.get('completion_evidence'): current['service_state']='completion_unknown'
            if counter_state(row)=='loaded': current['counter_loaded']=True
            counting=reviewed_fact(row,'cash_counting',seen,banknotes_visible=True,
                same_group=True,visible_counting_motion=True)
            if counting:
                current['counted']=True
                current['counting_evidence']=counting
            # One group reaching a customer/storage cannot account for another group
            # still in the work area. Visible residual custody reopens disposition.
            if custody:
                current['disposition']=None
                current['remaining_locations']=sorted(custody)
            disposition=None
            if locations & {'safe','locked_drawer'}:
                disposition='visible_in_storage'
            if 'visitor_hand' in locations and 'desk_hand' in old:
                disposition='possible_return'
            if disposition:
                current['evidence'].append(row['source'])
                if custody: current['partial_disposition']=disposition
                else:
                    current['disposition']=disposition
                    current['remaining_locations']=[]
                    current['end']=row['seconds']
            visible_disposition=reviewed_fact(row,'cash_disposition',seen,
                banknotes_visible_before=True,same_group=True,transfer_visible=True,
                destination_visible=True,no_visible_residual_group=True)
            if visible_disposition:
                destination=row['episode_evidence']['cash_disposition'].get('destination')
                clear_residual=any(x.get('certainty')=='clear' and x.get('location') in custody for x in money)
                if destination in ('visitor_hand','safe','locked_drawer') and not clear_residual:
                    current['disposition']='sequence_return' if destination=='visitor_hand' else 'sequence_storage'
                    current['disposition_evidence']=visible_disposition
                    current['remaining_locations']=[]
                    current['end']=row['seconds']
            # A missed visitor detection is not a departure or a completed visit.
            if departure and current.get('completion_evidence'):
                if current['state']=='received_hypothesis' and clear_table_cash and not current['disposition']:
                    unsecured_absence=row
            if current['disposition']: unsecured_absence=None
            if new_customer and presence=='yes' and current.get('completion_evidence'):
                trace=[r for r in seen if current['receipt_seconds'] is not None and r['seconds']>=current['receipt_seconds']]
                clear_trace=bool(trace) and all(not r.get('parse_error') and not r.get('crop_review',{}).get('parse_error') and
                    any(x.get('kind')=='cash' and x.get('certainty')=='clear' for x in r.get('objects',[])) for r in trace)
                linked=continuity and current['receipt_seconds'] is not None and continuity['bounds_seconds'][0]<=current['receipt_seconds']
                if unsecured_absence is not None and linked and clear_trace and not current['disposition'] and clear_table_cash and not current.get('between_visits_candidate'):
                    add('cash_left_unsecured_between_visits',row['seconds'],
                        'После видимого завершения обслуживания и ухода клиента прослеживаемая группа '
                        'ранее принятых наличных остаётся на столе при начале обслуживания другого клиента. '
                        'Кандидат требует проверки исходной последовательности.',
                        current['evidence']+[unsecured_absence['source'],row['source']],'NB231_20_candidate',
                        [unsecured_absence['seconds'],row['seconds']])
                    current['between_visits_candidate']=row['seconds']
                # Do not silently resolve old cash just because another person appeared.
                current['next_service_seconds']=row['seconds']
        episode_states.append(dict(seconds=row['seconds'],customer=customer,
            service=current['service_state'] if current else ('visible_completion' if completion else 'completion_unknown'),
            departure_evidence=departure,completion_evidence=completion))
        previous=row
    for event in events:
        if event['type']=='object_at_pocket':
            facts=[reviewed_fact(r,'pocket_transfer',seen,banknotes_visible=True,same_group=True,
                pocket_opening_visible=True,movement_into_pocket=True) for r in seen]
            facts=[f for f in facts if f and f['bounds_seconds'][0]<=event['seconds']<=f['bounds_seconds'][1]]
            if not facts: continue
            add('cash_at_personal_pocket',event['seconds'],
                'Последовательность кадров показывает перенос банкноты к видимому отверстию кармана. '
                'Кандидат требует проверки; намерение не установлено.',
                facts[0]['evidence_frames'],'project_cash_custody',facts[0]['bounds_seconds'])
        if event['type']=='separating_bundle':
            # Splitting/straightening alone is a normal possible operation, not a violation.
            pass
    unresolved=[t for t in transactions if not t['disposition']]
    parse_times=[r['seconds'] for r in rows if r.get('parse_error') or
                 (r.get('crop_review') or {}).get('parse_error')]
    abstention_reasons=[]
    if not rows: abstention_reasons.append(dict(code='no_observations'))
    if partial: abstention_reasons.append(dict(code='partial_analysis'))
    if parse_times:
        abstention_reasons.append(dict(code='unreadable_observations',seconds=parse_times))
    paper_times=[r['seconds'] for r in rows if not r.get('parse_error') and
                 (any(x.get('kind')=='paper_unknown' for x in r.get('objects',[])) or
                  any(x.get('object')=='paper_unknown' for x in r.get('contacts',[])))]
    if paper_times:
        abstention_reasons.append(dict(code='unresolved_paper_identity',scope='episode',
            seconds=paper_times,missing=['cash_identity'],
            detail='Paper-like objects were not resolved as banknotes or non-cash objects; absence of cash detections cannot establish a clean result.'))
    for transaction in unresolved:
        missing=[]
        if transaction['cash_identity']=='possible_presence':
            missing.append('cash_identity')
        else:
            if transaction['state']=='origin_unobserved': missing.append('cash_origin')
            if not transaction.get('counted'): missing.append('counting_or_handling')
            if not transaction.get('disposition'): missing.append('cash_disposition')
            if transaction.get('service_state')=='completion_unknown': missing.append('service_completion')
        abstention_reasons.append(dict(code='unresolved_cash_episode',
            episode_id=transaction['episode_id'],scope='episode',
            start_seconds=transaction['start'],missing=missing,
            end_seconds=transaction.get('last_observed_seconds',transaction['start']),
            remaining_locations=transaction.get('remaining_locations',[])))
    verdict='violation_suspected' if violations else ('insufficient_evidence' if not rows or partial or unresolved or
        parse_times or paper_times else 'no_violation_observed')
    result=dict(Status='partial' if partial else 'completed',Verdict=verdict,violations=violations,
        transactions=transactions,unresolved_transactions=len(unresolved),
        abstention_reasons=abstention_reasons,
        episode_states=episode_states,
        scope='Implemented visual project rules only, not all legal requirements',
        limitations=[
            'Safe opening alone never proves cash was stored. A closed door does not prove a lock is engaged.',
            'Cash remaining at clip end does not establish a violation without operation completion evidence.',
            'Cash first seen in the work area has unobserved origin; disappearance does not resolve its disposition.',
            'One visible return/storage group does not account for cash still visible in the work area.',
            'Visitor nondetection/reappearance does not establish departure, service completion or a different customer.',
            'Between-customer findings require explicit sequence evidence for departure, completion, a different customer and continuous cash-group identity.',
            'The supplied rule summary is the project source; legal currency and applicability were not independently verified.',
            'Organizer verdicts and timestamps are evaluation-only and are never input to this assessment.'])
    return apply_recording_context(result,recording or recording_context(partial=partial))
