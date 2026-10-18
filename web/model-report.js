import {h,externalLink} from './dom.js';
import {formatTime} from './player.js';

const stamp=value=>value==null?'не установлено':formatTime(Number(value),true);
const interval=row=>`${stamp(row.start_seconds)} — ${stamp(row.end_seconds)}`;

export function eventDetails(event,seek){
  const root=h('div',{class:'model-event-details'});
  if(event.requires_review||event.certainty==='possible')root.append(h('p',{class:'review-reason'},'Возможное нарушение · требуется проверка человеком'));
  if(event.timeline_status){
    const labels={review_interval_not_confirmed_event:'Интервал проверки: точные границы события не подтверждены',single_observation_only:'Зафиксирован отдельный момент; начало и конец не установлены',observed_candidate_interval:'Наблюдаемый интервал возможного события'};
    root.append(h('p',{},labels[event.timeline_status]||event.timeline_status));
    for(const [label,value] of [['Начало',event.event_start_seconds],['Момент',event.key_moment_seconds??event.timecode_sec],['Конец',event.event_end_seconds]]){
      root.append(value==null?h('span',{class:'model-time'},`${label}: не установлено`):h('button',{type:'button',class:'event-time model-time',onclick:()=>seek(value)},`${label}: ${stamp(value)}`));
    }
  }
  const basis=event.normative_basis;
  if(basis){
    const section=h('details',{},h('summary',{},'Норма и обоснование'));
    section.append(h('p',{},basis.act||'Нормативное основание не установлено',basis.provision?` · ${basis.provision}`:''));
    if(basis.url)section.append(h('p',{},externalLink(basis.url,'Открыть нормативный источник')));
    if(basis.note)section.append(h('p',{},basis.note));
    if(basis.status!=='verified')section.append(h('p',{class:'review-reason'},'Применимость нормы требует проверки. Это предварительная оценка, не юридическое заключение.'));
    root.append(section);
  }
  return root;
}

export function modelReport(result,run,seek){
  const root=h('section',{class:'model-report','aria-label':'Подробный отчёт Qwen'});
  if(result.analysis_scope?.mode!=='datathon')return root;
  root.append(h('h3',{},'Отчёт нашей модели'));
  if(result.preliminary)root.append(h('p',{class:'review-reason'},'Предварительная оценка · окончательное решение принимает проверяющий'));
  if(result.decision_summary)root.append(h('p',{},result.decision_summary));
  if(result.analysis_status)root.append(h('p',{},`Статус анализа: ${{completed:'завершён',failed:'ошибка',processing:'в работе'}[result.analysis_status]||result.analysis_status}`));
  const moments=result.moment_assessments||[];
  if(moments.length){
    const table=h('table',{class:'model-table'},h('thead',{},h('tr',{},['Интервал','Результат','Влияет на вердикт','Что произошло / основание'].map(x=>h('th',{scope:'col'},x)))));
    const body=h('tbody',{});
    for(const row of moments){
      const labels={possible_violation:'Возможное нарушение',no_violation_observed:'Нарушение в этом моменте не выявлено',analysis_error:'Ошибка анализа'};
      const time=row.start_seconds==null?h('span',{},interval(row)):h('button',{type:'button',class:'event-time',onclick:()=>seek(row.start_seconds)},interval(row));
      body.append(h('tr',{},h('td',{},time),h('td',{},labels[row.verdict]||row.label||row.verdict),h('td',{},row.affects_verdict===true?'Да':row.affects_verdict===false?'Нет':'Не установлено'),h('td',{},row.reason||'',row.sampling_scope==='evaluated_frames_only'?h('p',{class:'event-meta'},'Вывод относится к просмотренным кадрам.'):null)));
    }
    table.append(body);root.append(h('h4',{},'Моменты по хронологии'),h('div',{class:'model-table-scroll'},table));
  }
  const issues=result.review_issues?.length?result.review_issues:result.abstention_reasons||[];
  if(issues.length){
    const list=h('details',{},h('summary',{},`Неопределённости и их влияние · ${issues.length}`));
    for(const issue of issues){
      const bounds=issue.bounds_seconds;
      list.append(h('article',{class:'model-question'},h('strong',{},issue.code||'Проверить момент'),h('p',{},issue.description||''),h('p',{},issue.affects_verdict===true?'Влияет на вердикт → возможное нарушение':issue.affects_verdict===false?'Не влияет на общий вердикт':'Влияние на вердикт не установлено'),issue.impact_reason?h('p',{},issue.impact_reason):null,Array.isArray(bounds)&&bounds[0]!=null?h('button',{type:'button',class:'event-time',onclick:()=>seek(bounds[0])},`${stamp(bounds[0])} — ${stamp(bounds[1])}`):null));
    }
    root.append(list);
  }
  if(result.optional_review_failures?.length){
    root.append(h('details',{},h('summary',{},`Ошибки дополнительных проверок · ${result.optional_review_failures.length}`),result.optional_review_failures.map(x=>h('p',{},`${stamp(x.anchor_seconds)} · ${x.question}: ${x.error}${x.retry_attempted?' · повторная попытка выполнена':''}`))));
  }
  if(result.stage_seconds&&Object.keys(result.stage_seconds).length)root.append(h('details',{},h('summary',{},'Время работы'),Object.entries(result.stage_seconds).map(([name,seconds])=>h('p',{},`${name}: ${Number(seconds).toFixed(2)} с`))));
  root.append(h('button',{type:'button',class:'game-button',onclick:()=>{
    const blob=new Blob([JSON.stringify({job_id:run.job_id||run.id,status:run.status||'done',result},null,2)],{type:'application/json'});
    const url=URL.createObjectURL(blob),link=h('a',{href:url,download:`OKA-${run.job_id||run.id||'report'}.json`});
    document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }},'Скачать подробный отчёт JSON'));
  return root;
}
