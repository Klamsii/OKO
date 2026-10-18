import {formatTime} from './player.js';
import {$,h,imageSrc,externalLink} from './dom.js';
import {eventDetails} from './model-report.js';

export function normalizedEvents(result){
  const normalize=(event,index,review=false)=>{
    const rule=event.rule||{},time=Number(event.timecode??event.timecode_sec??0);
    return {...event,rule,id:String(event.event_id||event.id||`${review?'r':'v'}-${rule.id||event.type||'event'}-${time}-${index}`),
      type:rule.title||event.type||'Эпизод',timecode_sec:Number.isFinite(time)?time:0,
      evidence_frames:event.frames||event.evidence_frames||[],isReview:review};
  };
  return {violations:(result?.violations||[]).map((event,index)=>normalize(event,index)),review:(result?.review||[]).map((event,index)=>normalize(event,index,true))};
}

export class EventChat{
  constructor(player,{onEvent=()=>{}}={}){
    this.player=player;this.onEvent=onEvent;this.log=$('chat-log');this.indicator=$('new-events');this.reviewed=new Set();
    this.indicator.addEventListener('click',()=>{this.log.scrollTo({top:this.log.scrollHeight,behavior:'auto'});this.indicator.hidden=true;});
    this.log.addEventListener('scroll',()=>{if(this.atBottom())this.indicator.hidden=true;});
  }
  atBottom(){return this.log.scrollHeight-this.log.scrollTop-this.log.clientHeight<50;}
  clear(){this.log.replaceChildren(h('div',{class:'chat-empty'},h('span',{class:'chat-empty-glyph'},'>_'),h('p',{},'Здесь появятся отмеченные эпизоды')));$('event-count').textContent='0';this.indicator.hidden=true;}
  setReviewed(ids=[]){this.reviewed=new Set(ids);for(const entry of this.log.querySelectorAll('[data-event-id]'))entry.classList.toggle('event-reviewed',this.reviewed.has(entry.dataset.eventId));}
  render(result,{reviewedIds=[]}={}){
    const bottom=this.atBottom(),position=this.log.scrollTop,events=normalizedEvents(result);this.log.replaceChildren();
    $('event-count').textContent=String(events.violations.length);this.reviewed=new Set(reviewedIds);
    if(!events.violations.length)this.log.append(h('div',{class:'chat-empty'},h('span',{class:'chat-empty-glyph'},'✓'),h('p',{},result?.degraded?'Анализ выполнен с ограничениями. Проверьте замечания.':'По включённым детекторам отметок нет. Это не полный аудит кассовых операций.')));
    for(const [index,event]of events.violations.entries())this.log.append(this.card(event,index));
    if(events.review.length){const review=h('details',{class:'review-episodes'},h('summary',{},`На проверку человеку · ${events.review.length}`),h('p',{class:'review-note'},'Сомнительные эпизоды не входят в ответ API жюри.'));events.review.forEach((event,index)=>review.append(this.card(event,index)));this.log.append(review);}
    if(bottom)this.log.scrollTop=this.log.scrollHeight;else{this.log.scrollTop=position;this.indicator.hidden=false;}
  }
  card(event,index){
    const rule=event.rule,entry=h('article',{class:`chat-entry${event.isReview?' review-entry':''}${this.reviewed.has(event.id)?' event-reviewed':''}`,'data-event-id':event.id});
    const time=h('button',{class:'event-time',type:'button','aria-label':`Перейти к ${formatTime(event.timecode_sec,true)}`,onclick:()=>{
      this.player.seek(event.timecode_sec);for(const node of this.log.querySelectorAll('.selected'))node.classList.remove('selected');entry.classList.add('selected');this.onEvent(event.id);
    }},`[${formatTime(event.timecode_sec,true)}]`);
    entry.append(h('span',{class:'chat-speaker'},`<${event.isReview?'Проверить':'Эпизод'}_${String(index+1).padStart(2,'0')}>`),time,h('h3',{class:'event-type'},event.type));
    if(event.observed)entry.append(h('p',{class:'event-description'},event.observed));
    else if(event.description)entry.append(h('p',{class:'event-description'},event.description));
    if(event.review_reason)entry.append(h('p',{class:'review-reason'},event.review_reason));
    if(rule.why)entry.append(h('p',{class:'event-why'},rule.why));
    const strip=h('div',{class:'evidence-thumbnails'});
    event.evidence_frames.forEach((frame,index)=>{
      const src=imageSrc(frame.image_base64);if(!src)return;
      const caption=`${formatTime(Number(frame.timestamp_sec),true)} · ${event.type}`;
      strip.append(h('button',{type:'button',class:'evidence-thumb','aria-label':`Открыть ${caption}`,onclick:()=>{
        $('evidence-image').src=src;$('evidence-image').alt=caption;$('evidence-caption').textContent=caption;$('evidence-dialog').showModal();
      }},h('img',{src,alt:caption,loading:'lazy'}),h('span',{},formatTime(Number(frame.timestamp_sec),true))));
    });
    if(strip.children.length)entry.append(strip);
    if(event.timeline_status||event.normative_basis)entry.append(eventDetails(event,time=>this.player.seek(time)));
    if(!event.normative_basis&&(rule.id||rule.citation||rule.refs?.length)){
      const details=h('details',{class:'rule-details'},h('summary',{},'Правило и источник'));
      if(rule.citation)details.append(h('p',{},rule.citation));
      for(const ref of rule.refs||[])if(ref.verified)details.append(h('p',{},externalLink(ref.url,`${ref.source||''}, ${ref.point||''}`),ref.quote?h('q',{},ref.quote):null));
      if(!(rule.refs||[]).some(ref=>ref.verified))details.append(h('p',{},'Нет сопоставленного проверенного пункта нормы.'));
      entry.append(details);
    }
    const meta=[];if(Number.isFinite(event.confidence))meta.push(`Уверенность ${Math.round(event.confidence*100)}%`);
    if(event.t_end!=null&&event.t_end>event.timecode_sec)meta.push(`До ${formatTime(event.t_end,true)}`);
    if(event.source)meta.push(`Детектор: ${event.source}`);if(meta.length)entry.append(h('p',{class:'event-meta'},meta.join(' · ')));
    return entry;
  }
}
