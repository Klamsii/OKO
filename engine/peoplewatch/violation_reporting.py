"""Present supported candidates with honest temporal and legal provenance."""
from .compliance import timecode


def _basis(rule_id, kind):
    if rule_id == 'NB231_20_candidate' or kind == 'cash_left_unsecured_between_visits':
        return dict(status='requires_legal_review',act='Постановление Правления НБРК № 231',
                    provision='пункт 20 Правил',
                    url='https://adilet.zan.kz/rus/docs/V1900019680',
                    note='Пункт касается хранения ранее принятых денег в закрывающемся устройстве; нужно подтвердить, что видимая группа денег принята от предыдущего клиента и остаётся вне такого устройства.')
    if kind == 'employee_left_during_service':
        return dict(status='requires_legal_review',act='Постановление Правления НБРК № 231',
                    provision='пункт 22 Правил',
                    url='https://adilet.zan.kz/rus/docs/V1900019680',
                    note='Пункт запрещает кассовому работнику покидать рабочее место до окончания обслуживания клиента; нужны подтверждение роли работника, ухода именно с рабочего места и незавершённого обслуживания.')
    return dict(status='not_established',act=None,provision=None,url=None,
                note='Кадры и свод проекта сами по себе не устанавливают норму права.')


def describe_violations(violations):
    """Keep original evidence; label boundaries as observed, never invent episode duration."""
    result=[]
    for violation in violations:
        item=dict(violation)
        bounds=item.get('time_interval_seconds') or [None,None]
        start,end=bounds if len(bounds)==2 else (None,None)
        moment=item.get('seconds')
        observed_span=(start is not None and end is not None and end>start)
        item['event_start_seconds']=start if observed_span else None
        item['event_start']=timecode(start) if observed_span else None
        item['key_moment_seconds']=moment
        item['key_moment']=timecode(moment) if moment is not None else None
        item['event_end_seconds']=end if observed_span else None
        item['event_end']=timecode(end) if observed_span else None
        item['event_duration_seconds']=end-start if observed_span else None
        item['timeline_status']='observed_candidate_interval' if observed_span else 'single_observation_only'
        if item.get('timing_status')=='review_interval_not_confirmed_event':
            item['timeline_status']='review_interval_not_confirmed_event'
        item['normative_basis']=_basis(item.get('rule_id'),item.get('type'))
        result.append(item)
    return result
