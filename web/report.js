import {$,h,imageSrc,externalLink,copyText,finite} from './dom.js';
import {formatTime} from './player.js';
import {normalizedEvents} from './chat.js';
import {segmentedReview} from './controls.js';
import {analysisScope} from './analysis-scope.js';
import {modelReport} from './model-report.js';

export class ReportView{
  constructor({player,request,onSeek=()=>{},onSave=()=>{},onReview=()=>{}}){
    Object.assign(this,{player,request,onSeek,onSave,onReview});this.revision=0;this.current=null;this.fullJson='';
    this.root=$('report-tools');if(!this.root){this.root=h('div',{id:'report-tools',class:'report-tools'});document.querySelector('.video-column').append(this.root);}
    this.root.hidden=true;
    this.meta=h('p',{class:'report-meta'});this.notes=h('div',{class:'report-notes',role:'status'});
    this.saveStatus=h('span',{class:'save-status'});this.retry=h('button',{type:'button',class:'game-button',onclick:()=>this.onSave()},'Повторить сохранение');
    this.review=segmentedReview(value=>this.onReview(value));
    this.viewer=h('figure',{class:'timeline-viewer',hidden:true});this.viewerImage=h('img',{alt:'Кадр видео'});this.viewerCaption=h('figcaption',{});this.viewer.append(this.viewerImage,this.viewerCaption);
    this.timeline=h('div',{class:'analysis-timeline',role:'group','aria-label':'Таймлайн видео'});
    this.tip=h('figure',{class:'timeline-preview',hidden:true});this.tipImage=h('img',{alt:'Предпросмотр кадра'});this.tipCaption=h('figcaption',{});this.tip.append(this.tipImage,this.tipCaption);
    document.body.append(this.tip);this.tipAnchor=null;
    this.scale=h('p',{class:'timeline-scale'});
    this.pre=h('pre',{class:'report-json',tabindex:0});this.copy=h('button',{type:'button',class:'game-button',disabled:true,onclick:()=>copyText(this.fullJson,this.copy)},'Скопировать полный JSON');
    this.json=h('section',{class:'report-json-details','aria-label':'Ответ API для этой проверки'},h('h3',{},'Ответ API для этой проверки'),h('p',{class:'report-json-note'},'Base64 сокращён на экране. В копии JSON — полные кадры JPEG.'),this.copy,this.pre);
    this.root.append(this.meta,this.notes,h('div',{class:'report-actions'},h('div',{class:'report-save-state'},this.saveStatus,this.retry),h('div',{class:'report-review-field'},h('span',{class:'control-label'},'Просмотр человеком'),this.review.element)),
      h('h3',{},'Таймлайн'),h('p',{class:'timeline-legend'},'Красный — отмеченный эпизод · золотой — на проверку · серый — движение'),this.timeline,this.scale,this.viewer,this.json);
    window.addEventListener('scroll',()=>this.hidePreview(),true);window.addEventListener('resize',()=>this.hidePreview());
    window.addEventListener('hashchange',()=>this.hidePreview());document.addEventListener('visibilitychange',()=>{if(document.hidden)this.hidePreview();});
    player.subscribePosition(position=>this.mark(position));
    player.video.addEventListener('error',()=>{if(this.current)this.showFrame(finite(player.video.currentTime));});
    player.video.addEventListener('loadedmetadata',()=>{if(this.current&&player.available)this.viewer.hidden=true;});
  }
  clear(){this.revision++;this.current=null;this.fullJson='';this.root.hidden=true;this.timeline.replaceChildren();this.pre.textContent='';this.copy.disabled=true;this.hidePreview();this.viewer.hidden=true;}
  render(result,run={}){
    const revision=++this.revision;this.current={result,run};this.root.hidden=false;
    const duration=finite(result.video?.duration_sec??run.duration_sec),timing=finite(result.timings?.total_sec);
    this.meta.textContent=[run.filename||'',duration?`Длительность ${formatTime(duration)}`:'',timing?`Обработка ${timing.toFixed(1)} с`:'',`Проверка ${run.job_id||run.id||''}`].filter(Boolean).join(' · ');
    this.notes.replaceChildren();const scope=analysisScope({...run,result});
    if(scope?.label)this.notes.append(h('p',{class:'report-scope-label'},scope.label));
    if(scope?.verdict_labels?.[result.verdict])this.notes.append(h('p',{class:'report-scope-verdict'},scope.verdict_labels[result.verdict]));
    const method=h('details',{class:'report-method'},h('summary',{},'Что проверено и как'));
    for(const limitation of scope?.limitations||[])method.append(h('p',{},String(limitation)));
    for(const note of result.notes||[])if(!(scope?.limitations||[]).includes(note))method.append(h('p',{},String(note)));
    if(result.degraded)this.notes.prepend(h('p',{},'Проверка выполнена с ограничениями. Отрицательный результат относится только к доступным детекторам.'));
    if(result.coverage_sec!=null)method.append(h('p',{},`Покрытие: ${finite(result.coverage_sec).toFixed(1)} из ${duration?duration.toFixed(1):'?'} с`));
    if(result.detectors?.length&&!(result.notes||[]).some(note=>String(note).includes('Активные детекторы:')))method.append(h('p',{},`Активные детекторы: ${result.detectors.map(item=>typeof item==='string'?item:item.name||item.id||'—').join(', ')}`));
    if(method.children.length>1)this.notes.append(method);
    this.root.querySelector('.model-report')?.remove();this.json.before(modelReport(result,run,time=>{this.player.seek(time);this.mark(time);if(!this.player.available)this.showFrame(time);}));
    this.hidePreview();this.setArchiveState(run);this.review.value=run.review_status||'not_reviewed';this.buildTimeline(result);this.renderJson(run,revision);
    if(!this.player.available)this.showFrame(finite(run.last_playback_sec));else this.viewer.hidden=true;
  }
  setArchiveState(run){
    const status=run.archive_status||run.save_status;
    const labels={saved:'Сохранено в истории',saving:'Сохранение…',partial:'Сохранено частично: проверьте доступность видео',save_failed:'Не удалось сохранить проверку'};
    this.saveStatus.textContent=labels[status]||'Состояние сохранения уточняется';this.retry.hidden=!['partial','save_failed'].includes(status);
  }
  async renderJson(run,revision){
    this.fullJson='';this.copy.disabled=true;this.pre.textContent='Загрузка JSON…';
    try{
      const id=run.job_id||run.id,data=run.api_result||await this.request(`/ui/v1/jobs/${encodeURIComponent(id)}/api`);
      if(this.revision!==revision)return;this.fullJson=JSON.stringify(data,null,2);this.copy.disabled=false;
      this.pre.textContent=JSON.stringify(data,(key,value)=>key==='image_base64'&&typeof value==='string'&&value.length>70?`${value.slice(0,24)}… (${Math.round(value.length*.75/1024)} КБ JPEG)`:value,2);
    }catch(error){if(this.revision===revision)this.pre.textContent=`JSON недоступен: ${error.message}`;}
  }
  frameAt(second){
    const thumbs=this.current?.result.thumbs||[],value=thumbs[Math.max(0,Math.min(Math.floor(second),thumbs.length-1))];
    if(value)return {src:imageSrc(typeof value==='string'?value:value?.image_base64||''),second:typeof value==='object'&&value.timestamp_sec!=null?value.timestamp_sec:Math.floor(second)};
    const frames=(this.current?.result.violations||[]).flatMap(event=>event.frames||event.evidence_frames||[]).filter(frame=>frame.image_base64&&Number.isFinite(Number(frame.timestamp_sec)));
    const frame=frames.sort((a,b)=>Math.abs(a.timestamp_sec-second)-Math.abs(b.timestamp_sec-second))[0];
    return frame?{src:imageSrc(frame.image_base64),second:frame.timestamp_sec}:null;
  }
  showFrame(second){const frame=this.frameAt(second);this.viewer.hidden=!frame;if(frame){this.viewerImage.src=frame.src;this.viewerCaption.textContent=`Ближайший сохранённый кадр ${formatTime(frame.second,true)}. Исходный кодек может не поддерживаться браузером.`;}}
  hidePreview(){this.tip.hidden=true;this.tipAnchor=null;}
  showPreview(button,second,event){
    const frame=this.frameAt(second);if(!frame){this.hidePreview();return;}
    this.tipAnchor=button;this.tipImage.src=frame.src;this.tipCaption.textContent=`Кадр ${formatTime(frame.second,true)}${event?` · ${event.type}`:''}`;this.tip.hidden=false;
    const rect=button.getBoundingClientRect(),box=this.tip.getBoundingClientRect(),gap=10,pad=12;
    this.tip.style.left=`${Math.max(pad,Math.min(rect.left+rect.width/2-box.width/2,innerWidth-box.width-pad))}px`;
    this.tip.style.top=`${Math.max(pad,Math.min(rect.top>=box.height+gap+pad?rect.top-box.height-gap:rect.bottom+gap,innerHeight-box.height-pad))}px`;
  }
  buildTimeline(result){
    this.timeline.replaceChildren();const events=normalizedEvents(result),duration=Math.max(1,Math.ceil(finite(result.video?.duration_sec,(result.activity||[]).length||1)));
    this.step=Math.max(1,Math.ceil(duration/240));const count=Math.ceil(duration/this.step),activity=result.activity||[];
    for(let index=0;index<count;index++){
      const start=index*this.step,end=Math.min(duration,start+this.step),overlaps=event=>event.timecode_sec<end&&finite(event.t_end,event.timecode_sec)>=start;
      const bad=events.violations.find(overlaps),review=events.review.find(overlaps),event=bad||review;
      let movement=0;for(let second=start;second<end;second++)movement=Math.max(movement,finite(activity[second]));
      const button=h('button',{type:'button',class:`timeline-slot${bad?' is-bad':review?' is-review':''}`,'data-index':index,
        'aria-label':`${formatTime(start)}${event?` · ${event.type}`:''}`,title:`${formatTime(start)}${event?` · ${event.type}`:''}`});
      button.style.setProperty('--activity-color',`rgb(${Math.round(62+movement*88)},${Math.round(56+movement*83)},${Math.round(65+movement*86)})`);
      const preview=()=>this.showPreview(button,start,event);
      button.addEventListener('mouseenter',preview);button.addEventListener('focus',preview);
      button.addEventListener('mouseleave',()=>{if(document.activeElement!==button)this.hidePreview();});button.addEventListener('blur',()=>this.hidePreview());
      button.addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();this.hidePreview();}});
      button.addEventListener('click',()=>{this.player.seek(start);this.mark(start);if(!this.player.available)this.showFrame(start);if(event)this.onSeek(event.id);});
      this.timeline.append(button);
    }
    this.scale.textContent=`00:00 — ${formatTime(duration)} · клетка ${this.step} с`;this.mark(finite(this.current?.run.last_playback_sec));
  }
  mark(second){
    if(!this.current)return;const index=Math.floor(second/(this.step||1));
    for(const button of this.timeline.children){const on=Number(button.dataset.index)===index;button.classList.toggle('is-current',on);button.style.outline=on?'2px solid #f5d88b':'';}
  }
}

export async function showHelp(request,caps={}){
  const dialog=$('info-dialog');let content=$('help-content');
  if(!content){content=h('div',{id:'help-content'});for(const child of [...dialog.children])if(!child.matches('[data-close]'))child.remove();dialog.append(content);}
  content.replaceChildren(h('h2',{},'Справка'),h('p',{},'Пробел — пауза; ←/→ — 5 секунд; Z — масштаб; M — звук видео; F — полный экран. Движение и звук можно выключить отдельно.'),h('p',{},'Автоматические отметки требуют проверки человеком. Отметка о просмотре не подтверждает нарушение.'));
  const scope=caps.analysis_scope;if(scope){const scopeInfo=h('section',{class:'help-analysis-scope'},h('h3',{},scope.label||'Возможности проверки'));for(const limitation of scope.limitations||[])scopeInfo.append(h('p',{},String(limitation)));content.append(scopeInfo);}
  if(!dialog.open)dialog.showModal();
  const api=h('details',{},h('summary',{},'API для жюри'));
  const curl=`curl -X POST ${location.origin}/api/v1/analyze -H "X-API-Key: <ключ>" -F "file=@video.mov"\n\ncurl ${location.origin}/api/v1/jobs/<job_id> -H "X-API-Key: <ключ>"`;
  const button=h('button',{type:'button',class:'game-button'},'Скопировать пример');button.addEventListener('click',()=>copyText(curl,button));
  api.append(h('pre',{},curl),button,h('p',{},'POST возвращает job_id. GET: processing, done или failed; verdict: violation / no_violation. Нарушение: type, timecode_sec, description, evidence_frames (timestamp_sec, JPEG image_base64).'));
  api.append(h('p',{},`Форматы: ${(caps.formats||[]).join(', ')}. До ${Math.floor(finite(caps.max_upload_bytes)/1048576)} МБ. Бюджет обработки: ${finite(caps.deadline_sec)} с.`),h('p',{},'Ошибки: 400 — пустой файл; 401 — ключ/сессия; 404 — задача не найдена; 413 — размер; 415 — формат; 503 — очередь заполнена. Ошибка анализа не означает отсутствие нарушений.'));
  content.append(api);const catalogRoot=h('details',{},h('summary',{},'Нарушения и нормы'),h('p',{},'Загрузка каталога…'));content.append(catalogRoot);
  try{
    const catalog=await request('/ui/v1/catalog');catalogRoot.replaceChildren(h('summary',{},'Нарушения и нормы'),h('p',{},'A — видно однозначно; B — нужен чёткий кадр; C — только явные случаи. Наличие в каталоге не означает наличие активного детектора.'));
    const groups=[...new Set([...(catalog.groups||[]),'Прочее'])];
    for(const group of groups){const rules=(catalog.rules||[]).filter(rule=>rule.group===group);if(!rules.length)continue;const section=h('section',{class:'catalog-group'},h('h3',{},group));
      for(const rule of rules){const item=h('article',{class:'catalog-rule'},h('h4',{},`${rule.id} · ${rule.title} · ${rule.level}`),h('p',{},rule.hint||rule.why||''));
        for(const ref of rule.refs||[])item.append(h('p',{},externalLink(ref.url,`${ref.source} ${ref.point}${ref.verified?'':' · сверяется'}`),ref.quote?h('q',{},ref.quote):null));
        if(!(rule.refs||[]).some(ref=>ref.verified))item.append(h('p',{},'Без проверенной нормы: только на проверку человеку.'));section.append(item);
      }catalogRoot.append(section);
    }
    if(catalog.sources?.length)catalogRoot.append(h('h3',{},'Источники'),...(catalog.sources||[]).map(source=>h('p',{},externalLink(source.url,`${source.code} — ${source.title}`),' ',source.act||'')));
    if(catalog.not_claimed?.length)catalogRoot.append(h('h3',{},'Что система не утверждает'),h('ul',{},catalog.not_claimed.map(text=>h('li',{},text))));
  }catch(error){catalogRoot.replaceChildren(h('summary',{},'Нарушения и нормы'),h('p',{},`Каталог недоступен: ${error.message}`));}
}
