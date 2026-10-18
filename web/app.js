import {CONFIG} from './config.js';
import {AudioManager} from './audio.js';
import {Worlds} from './worlds.js';
import {VideoPlayer,formatTime} from './player.js';
import {EventChat} from './chat.js';
import {ReportView,showHelp} from './report.js';
import {ArchiveView,analysisLabels,assetAvailable} from './archive.js';
import {Credits} from './credits.js';
import {$,h,finite,terminal} from './dom.js';
import {analysisScope} from './analysis-scope.js';

const state={authenticated:false,apiKey:'',caps:{},file:null,displayRunId:null,generation:0,submitting:false,attempt:null,world:'overworld'};
const runs=new Map(),reviews=new Map();let navigationRevision=0,reviewTimer=null,restoring=false;
try{sessionStorage.removeItem('api_key');}catch{/* A disabled store does not prevent analysis. */}
for(const el of document.querySelectorAll('[data-project-name]'))el.textContent=CONFIG.projectName;
document.title=`${CONFIG.projectName} — проверка кассового видео`;

async function request(url,options={}){
  const headers=new Headers(options.headers||{});let body=options.body;
  if(body&&typeof body==='object'&&!(body instanceof FormData)){headers.set('Content-Type','application/json');body=JSON.stringify(body);}
  let response;
  try{response=await fetch(url,{...options,body,headers,credentials:'same-origin',cache:'no-store'});}catch(error){if(error.name==='AbortError')throw error;throw new Error('Нет соединения с сервисом. Повторите попытку; уже принятый анализ не запускается заново.');}
  let value={};const text=await response.text();if(text){try{value=JSON.parse(text);}catch{throw new Error('Сервис вернул неожиданный ответ.');}}
  if(!response.ok){
    const error=new Error(response.status===401?'Сессия завершена или ключ доступа неверен. Войдите снова.':typeof value.detail==='string'?value.detail:value.error||`Ошибка сервиса (${response.status}).`);error.status=response.status;
    if(response.status===401&&url!=='/ui/v1/session'&&state.authenticated){setAuthenticated(false);state.authTarget=state.world;$('welcome-error').textContent=error.message;$('welcome-error').hidden=false;setTimeout(()=>navigate('overworld').catch(showError),0);}
    throw error;
  }
  return value;
}

const audio=new AudioManager();
const player=new VideoPlayer({audio,onError:showError,onPosition:position=>recordPosition(position)});
const chat=new EventChat(player,{onEvent:id=>markEvent(id)});
const worlds=new Worlds({audio,onChange:world=>{state.world=world;if(world!=='nether')player.pause();document.querySelector('.game-nav').hidden=world==='credits';$('nav-history').hidden=world!=='nether';$('nav-analysis').hidden=world!=='end';}});
const credits=new Credits({audio,onFinish:()=>navigate('overworld').catch(showError)});
const report=new ReportView({player,request,onSeek:id=>markEvent(id),onSave:()=>retrySave().catch(showError),onReview:value=>updateReview(value)});
const archive=new ArchiveView({request,onOpen:id=>openRun(id),onReanalyze:id=>reanalyze(id),onDelete:id=>deletedRun(id),onAnalyze:()=>navigate('nether')});
const progress=h('progress',{id:'run-progress',max:1,value:0,'aria-label':'Ход анализа',hidden:true});$('analysis-status').append(progress);
const reconnect=h('button',{type:'button',class:'game-button',hidden:true,onclick:()=>resumeConnections()},'Восстановить связь');$('analysis-error').after(reconnect);

function showError(error){$('analysis-error').textContent=typeof error==='string'?error:error?.message||'Не удалось выполнить действие.';$('analysis-error').hidden=!error;}
function setAuthenticated(value){state.authenticated=value;$('api-key').required=!value;}
function setStatus(message,phase='ready'){$('status-text').textContent=message;$('analysis-status').dataset.state=phase;}
function checkName(){return state.caps.analysis_scope?.label||'Проверка видео';}
function displayContext(){return state.displayRunId?runs.get(state.displayRunId):null;}
function visible(ctx){return ctx&&state.displayRunId===ctx.id&&ctx.generation===state.generation;}
function rememberRun(id){try{if(id)sessionStorage.setItem('forte-active-run',id);else sessionStorage.removeItem('forte-active-run');}catch{/* Optional convenience only. */}}
function storedRun(){try{return sessionStorage.getItem('forte-active-run');}catch{return null;}}
function randomKey(){if(crypto.randomUUID)return crypto.randomUUID();const bytes=new Uint8Array(16);crypto.getRandomValues(bytes);return [...bytes].map(value=>value.toString(16).padStart(2,'0')).join('');}
function updateControls(){
  const ctx=displayContext(),running=ctx&&!terminal(ctx.data?.status)&&!ctx.unknown;
  $('upload-button').disabled=state.submitting;$('video-file').disabled=state.submitting;
  $('drop-zone').setAttribute('aria-disabled',String(state.submitting));
  $('analyze-button').disabled=state.submitting||!!running||(!state.file&&!assetAvailable(ctx?.data||{}));
  const cameraOnly=state.caps.analysis_scope?.mode==='camera_visibility';
  const label=state.submitting?'ОТПРАВКА…':state.displayRunId?'ПРОВЕРИТЬ СНОВА':cameraOnly?'ПРОВЕРИТЬ КАМЕРУ':'ПРОВЕРИТЬ ВИДЕО';
  const button=$('analyze-button');if(button.lastChild?.nodeType===Node.TEXT_NODE)button.lastChild.textContent=label;else button.append(document.createTextNode(label));
  if($('clear-video'))$('clear-video').disabled=state.submitting||(!state.file&&!state.displayRunId);
}

function applyCapabilities(info){
  state.caps=info.capabilities||info.caps||info;
  state.caps.analysis_scope=analysisScope(state.caps);
  const scope=state.caps.analysis_scope;
  if(scope?.mode==='datathon'){
    let health=$('model-health');if(!health){health=h('p',{id:'model-health',role:'status'});$('analysis-scope')?.after(health);}
    health.textContent='Проверяем подключение Qwen…';
    request('/ui/v1/model-health').then(data=>{health.textContent=data.message;}).catch(()=>{health.textContent='Не удалось проверить готовность Qwen.';});
  }
  const scopeNotice=$('analysis-scope');
  if(scopeNotice){scopeNotice.replaceChildren(h('span',{class:'scope-label'},scope?.label||'Проверка видео'),h('span',{},scope?.mode==='camera_visibility'?'Сейчас доступна проверка перекрытия камеры · V13. LLM не подключена.':scope?.mode==='datathon'?'Проверка действий с деньгами и кассовых операций. Возможные нарушения и спорные моменты — в подробном отчёте.':(scope?.limitations||[]).join(' ')||'Возможности проверки зависят от подключённых детекторов.'));}
  const formats=Array.isArray(state.caps.formats)?state.caps.formats:['.mov'];
  state.formats=formats.map(value=>String(value).toLowerCase().replace(/^\./,''));
  $('video-file').accept=state.formats.map(value=>'.'+value).join(',');
  const label=state.formats.map(value=>'.'+value.toUpperCase()).join(' / '),max=Math.floor(finite(state.caps.max_upload_bytes,100*1048576)/1048576);
  $('file-limits').textContent=`${label} · ДО ${max} МБ${state.caps.max_video_seconds?` · ВИДЕО ДО ${formatTime(finite(state.caps.max_video_seconds))}`:''} · БЮДЖЕТ ${finite(state.caps.deadline_sec,300)} С`;
  $('upload-button').setAttribute('aria-label',`UPLOAD — выбрать видео (${label})`);
  $('drop-zone').setAttribute('aria-label',`Область видео. Enter — выбрать; можно перетащить ${label}`);
  for(const tag of document.querySelectorAll('.drop-format'))tag.textContent=label;
  $('playback-error').textContent='Браузер не воспроизводит кодек этого видео. Сервер анализа может прочитать запись; результат проверки будет доступен после обработки.';
  if(info.local_asset_pack||state.caps.local_asset_pack)audio.useLocalPack(info.local_asset_pack||state.caps.local_asset_pack).catch(()=>{});
  updateControls();
}

const route=()=>{
  const hash=location.hash.slice(1),parts=hash.split('/');
  return {world:parts[0]==='history'?'end':parts[0]==='credits'?'credits':parts[0]==='analysis'?'nether':'overworld',runId:parts[0]==='analysis'&&parts[1]?decodeURIComponent(parts[1]):null};
};
async function navigate(world,push=true){
  if(['nether','end'].includes(world)&&!state.authenticated){state.authTarget=world;world='overworld';}
  const revision=++navigationRevision;if(world!=='credits')credits.stop({notify:false});
  await worlds.ready;if(revision!==navigationRevision)return;
  const hash=world==='nether'?`#analysis${state.displayRunId?'/'+encodeURIComponent(state.displayRunId):''}`:world==='end'?'#history':world==='credits'?'#credits':'#welcome';
  if(push&&location.hash!==hash)history.pushState({world,runId:state.displayRunId},'',hash);
  await worlds.go(world);if(revision!==navigationRevision)return;state.world=world;
  for(const [id,name]of [['nav-home','overworld'],['nav-analysis','nether'],['nav-history','end']])$(id)?.setAttribute('aria-current',name===world?'page':'false');
  if(world==='end'){await archive.load();$('archive-search').focus({preventScroll:true});}
  else if(world==='nether')$('upload-button').focus({preventScroll:true});else if(world==='overworld')$('api-key').focus({preventScroll:true});
}
window.addEventListener('popstate',()=>{const next=route();if(next.runId&&next.runId!==state.displayRunId&&state.authenticated)openRun(next.runId,{navigateTo:false}).then(()=>navigate(next.world,false)).catch(showError);else navigate(next.world,false).catch(showError);});
for(const [id,world]of [['nav-home','overworld'],['nav-analysis','nether'],['nav-history','end']])$(id)?.addEventListener('click',()=>navigate(world).catch(showError));
$('back-button').addEventListener('click',()=>navigate(state.world==='end'?'nether':'overworld').catch(showError));
$('brand-link').addEventListener('click',event=>{event.preventDefault();navigate('overworld').catch(showError);});
$('end-exit-button')?.addEventListener('click',async()=>{try{await navigate('credits');await credits.start();}catch(error){showError(error);}});
$('key-visibility')?.addEventListener('click',()=>{const visible=$('api-key').type==='password';$('api-key').type=visible?'text':'password';$('key-visibility').setAttribute('aria-label',visible?'Скрыть ключ':'Показать ключ');});
$('welcome-cancel').addEventListener('click',()=>{
  if($('start-button').disabled)return;
  $('api-key').value='';$('api-key').type='password';$('welcome-error').hidden=true;$('welcome-error').textContent='';state.authTarget='nether';$('api-key').focus({preventScroll:true});
});
const originalStart=$('start-button').innerHTML;
$('welcome-form').addEventListener('submit',async event=>{
  event.preventDefault();if($('start-button').disabled)return;
  const key=$('api-key').value.trim();if(!key&&state.authenticated){await navigate('nether');return;}if(!key)return;$('welcome-error').hidden=true;$('start-button').disabled=true;$('welcome-cancel').disabled=true;$('start-button').textContent='ПРОВЕРКА ДОСТУПА…';
  try{
    const info=await request('/ui/v1/session',{method:'POST',body:{api_key:key}});state.apiKey=key;setAuthenticated(true);applyCapabilities(info);$('api-key').value='';
    await afterLogin(state.authTarget||'nether');state.authTarget=null;
  }catch(error){$('welcome-error').textContent=error.message;$('welcome-error').hidden=false;}
  finally{$('start-button').innerHTML=originalStart;$('start-button').disabled=false;$('welcome-cancel').disabled=false;}
});

function selectFile(file){
  if(!file||state.submitting)return;const ext=(file.name.split('.').pop()||'').toLowerCase();
  if(!state.formats?.includes(ext)){showError(`Поддерживаются: ${(state.formats||['mov']).map(value=>'.'+value).join(', ')}.`);return;}
  if(!file.size){showError('Этот файл пуст. Выберите другую запись.');return;}
  if(file.size>finite(state.caps.max_upload_bytes,100*1048576)){showError(`Файл больше ${Math.floor(finite(state.caps.max_upload_bytes)/1048576)} МБ.`);return;}
  if(!canLeaveUnsaved())return;flushReview();state.generation++;state.displayRunId=null;rememberRun(null);state.file=file;state.attempt=null;
  showError('');report.clear();chat.clear();player.select(file);$('file-name').textContent=file.name;$('file-meta').textContent=`${(file.size/1048576).toFixed(1)} МБ · готово к проверке`;
  progress.hidden=true;reconnect.hidden=true;setStatus('Видео выбрано. Можно запустить проверку.','selected');updateControls();
}
$('upload-button').addEventListener('click',()=>$('video-file').click());
$('video-file').addEventListener('change',event=>{selectFile(event.target.files[0]);event.target.value='';});
$('drop-zone').addEventListener('keydown',event=>{if(event.key==='Enter'&&!state.submitting){event.preventDefault();$('video-file').click();}});
for(const name of ['dragenter','dragover'])$('drop-zone').addEventListener(name,event=>{event.preventDefault();if(!state.submitting)$('drop-zone').classList.add('drag-over');});
for(const name of ['dragleave','drop'])$('drop-zone').addEventListener(name,event=>{event.preventDefault();$('drop-zone').classList.remove('drag-over');});
$('drop-zone').addEventListener('drop',event=>selectFile(event.dataTransfer?.files[0]));
window.addEventListener('dragover',event=>event.preventDefault());window.addEventListener('drop',event=>event.preventDefault());

function upload(file,key,onProgress){
  return new Promise((resolve,reject)=>{
    const xhr=new XMLHttpRequest();xhr.open('POST','/ui/v1/analyze');xhr.setRequestHeader('Idempotency-Key',key);
    xhr.timeout=Math.max(30000,finite(state.caps.deadline_sec,300)*1000);
    xhr.upload.onprogress=event=>{if(event.lengthComputable)onProgress(event.loaded/event.total);};
    xhr.onload=()=>{let result;try{result=JSON.parse(xhr.responseText);}catch{reject(new Error('Не удалось прочитать ответ загрузки. Повторите отправку — ключ запроса сохранён.'));return;}
      if(xhr.status>=200&&xhr.status<300&&result.job_id)resolve(result);else{const error=new Error(result.detail||`Ошибка загрузки (${xhr.status}).`);error.status=xhr.status;reject(error);}};
    xhr.onerror=xhr.ontimeout=()=>reject(new Error('Ответ загрузки не получен. Повторите отправку: уже принятое задание не дублируется.'));
    const data=new FormData();data.append('file',file,file.name);xhr.send(data);
  });
}
async function startAnalysis(){
  if(state.submitting)return;const displayed=displayContext();if(displayed&&!terminal(displayed.data?.status)&&!displayed.unknown)return;
  if(!state.file){if(state.displayRunId)await reanalyze(state.displayRunId);return;}
  const generation=state.generation,file=state.file;
  const attempt=state.attempt&&state.attempt.file===file?state.attempt:{file,key:randomKey()};state.attempt=attempt;state.submitting=true;showError('');updateControls();
  setStatus('Загрузка видео…','uploading');progress.hidden=false;progress.value=0;
  try{
    const response=await upload(file,attempt.key,fraction=>{if(state.generation===generation){progress.value=fraction*.15;setStatus(`Загрузка: ${Math.round(fraction*100)}%`,'uploading');}});
    const id=String(response.job_id),ctx=ensureContext(id,{...response,filename:file.name});ctx.generation=generation;state.attempt=null;
    if(state.generation===generation){state.displayRunId=id;rememberRun(id);report.clear();chat.clear();history.replaceState({world:'nether',runId:id},'',`#analysis/${encodeURIComponent(id)}`);}
    poll(ctx);archive.refreshIfVisible();
  }catch(error){if(state.generation===generation){showError(error);setStatus('Отправка не подтверждена. Повтор использует тот же ключ запроса.','error');}if(error.status&&error.status>=400&&error.status<500)state.attempt=null;}
  finally{state.submitting=false;updateControls();}
}
$('analyze-button').addEventListener('click',()=>startAnalysis().catch(showError));

function ensureContext(id,data={}){
  let ctx=runs.get(id);if(!ctx){ctx={id,data:{},generation:-1,misses:0,rendered:false,deleted:false,timer:null,polling:false};runs.set(id,ctx);}ctx.data={...ctx.data,...data};return ctx;
}
function renderRun(ctx){
  if(!visible(ctx))return;const data=ctx.data,status=data.status||'unknown';
  $('file-name').textContent=data.filename||state.file?.name||'Видео';
  $('file-meta').textContent=[`Проверка ${ctx.id}`,data.duration_sec?formatTime(finite(data.duration_sec)):null].filter(Boolean).join(' · ');
  progress.hidden=terminal(status)||ctx.unknown;
  if(status==='done'&&data.result){
    if(!ctx.rendered){report.render(data.result,{...data,job_id:ctx.id});chat.render(data.result,{reviewedIds:reviews.get(ctx.id)?.reviewed_event_ids||data.reviewed_event_ids||[]});ctx.rendered=true;}
    else report.setArchiveState(data);
    const n=(data.result.violations||[]).length;setStatus(`${data.analysis_scope?.label||checkName()} завершена${data.result.degraded?' с ограничениями':''} · ${n} отметок`,'done');
  }else if(status==='failed'||status==='cancelled'||ctx.unknown||!['queued','processing'].includes(status)){
    report.clear();chat.clear();setStatus(ctx.unknown?'Связь прервана. Статус требует уточнения.':analysisLabels[status]||analysisLabels.unknown,'error');showError(data.error||ctx.connectionError||'Анализ не завершён. Это не результат без нарушений.');
  }else{
    const pct=Math.round(Math.max(0,Math.min(1,finite(data.progress)))*100);progress.value=.15+.85*pct/100;
    const started=new Date(data.created_at||0).getTime(),elapsed=Number.isFinite(started)&&started>0?Math.max(0,Math.floor((Date.now()-started)/1000)):null;
    const label=status==='queued'?`В очереди${data.queue_position?` · место ${data.queue_position}`:''}`:data.stage==='detect'?checkName():data.stage_label||checkName();
    setStatus(`${label} · ${pct}%${elapsed!==null?` · ${formatTime(elapsed)}`:''}`,status);
  }
  reconnect.hidden=!ctx.unknown;updateControls();
}
async function poll(ctx){
  if(ctx.deleted||ctx.polling)return;clearTimeout(ctx.timer);ctx.polling=true;
  try{
    const data=await request(`/ui/v1/jobs/${encodeURIComponent(ctx.id)}`);if(ctx.deleted)return;
    ctx.data={...ctx.data,...data};ctx.misses=0;ctx.unknown=false;
    if(!reviews.has(ctx.id))reviews.set(ctx.id,{last_playback_sec:finite(data.last_playback_sec),review_status:data.review_status||'not_reviewed',reviewed_event_ids:data.reviewed_event_ids||[]});
    if(!['queued','processing','done','failed','cancelled'].includes(data.status)){ctx.unknown=true;ctx.connectionError='Сервис вернул неизвестное состояние. Обновите статус без повторного анализа.';}
    renderRun(ctx);if(terminal(data.status)||ctx.unknown){archive.refreshIfVisible();if(terminal(data.status)&&!ctx.notified){ctx.notified=true;if(visible(ctx)&&data.status==='done')audio.play('complete');}return;}
    ctx.timer=setTimeout(()=>poll(ctx),CONFIG.pollIntervalMs||1500);
  }catch(error){
    if(ctx.deleted)return;ctx.misses++;ctx.connectionError=error.message;
    if(error.status===401||ctx.misses>=5){ctx.unknown=true;renderRun(ctx);}
    else ctx.timer=setTimeout(()=>poll(ctx),2000);
  }finally{ctx.polling=false;}
}
function resumeConnections(){for(const ctx of runs.values())if(ctx.unknown&&!ctx.deleted){ctx.unknown=false;ctx.misses=0;poll(ctx);}reconnect.hidden=true;}

function canLeaveUnsaved(){
  const data=displayContext()?.data;
  if(data?.result&&['partial','save_failed'].includes(data.archive_status))return confirm('Проверка сохранена не полностью. Можно остаться и нажать «Повторить сохранение». Убрать текущую копию из окна сейчас?');
  return true;
}
function clearWorkspace({force=false}={}){
  if(state.submitting)return false;const ctx=displayContext();
  if(!force&&!canLeaveUnsaved())return false;
  if(!force&&ctx&&!terminal(ctx.data.status)&&!confirm('Убрать видео из окна? Анализ продолжится в истории.'))return false;
  flushReview();state.generation++;state.displayRunId=null;state.file=null;state.attempt=null;rememberRun(null);player.clear();chat.clear();report.clear();
  $('video-file').value='';$('file-name').textContent='Выберите запись для проверки';$('file-meta').textContent='';showError('');progress.hidden=true;reconnect.hidden=true;setStatus('Готов к загрузке видео');updateControls();
  if(state.world==='nether')history.replaceState({world:'nether'},'','#analysis');return true;
}
$('clear-video')?.addEventListener('click',()=>clearWorkspace());

async function openRun(id,{navigateTo=true}={}){
  if(!canLeaveUnsaved())return;flushReview();const generation=++state.generation;state.displayRunId=null;state.file=null;state.attempt=null;player.clear();report.clear();chat.clear();showError('');
  const data=await request(`/ui/v1/runs/${encodeURIComponent(id)}`);if(generation!==state.generation)return;
  const ctx=ensureContext(id,data);ctx.generation=generation;ctx.rendered=false;ctx.unknown=false;state.displayRunId=id;rememberRun(id);
  reviews.set(id,{last_playback_sec:finite(data.last_playback_sec),review_status:data.review_status||'not_reviewed',reviewed_event_ids:data.reviewed_event_ids||[]});
  if(assetAvailable(data))player.loadSource(`/ui/v1/runs/${encodeURIComponent(id)}/video`,{position:finite(data.last_playback_sec)});
  else showError('Видео недоступно. Сохранённые отчёт и кадры остаются доступны.');
  renderRun(ctx);if(!terminal(data.status))poll(ctx);if(navigateTo)await navigate('nether');updateControls();
}
async function reanalyze(id){
  if(state.submitting)return;if(!canLeaveUnsaved())return;state.submitting=true;updateControls();
  // A retry after a lost response reuses its key; a later intentional run gets a fresh one.
  const ctx=ensureContext(id),key=ctx.reanalyzeKey||randomKey();ctx.reanalyzeKey=key;
  try{const response=await request(`/ui/v1/runs/${encodeURIComponent(id)}/reanalyze`,{method:'POST',headers:{'Idempotency-Key':key}});ctx.reanalyzeKey=null;await openRun(String(response.job_id));archive.refreshIfVisible();}
  finally{state.submitting=false;updateControls();}
}
function deletedRun(id){const ctx=runs.get(id);if(ctx){ctx.deleted=true;clearTimeout(ctx.timer);}reviews.delete(id);if(state.displayRunId===id)clearWorkspace({force:true});}
async function retrySave(){
  const id=state.displayRunId;if(!id)return;const generation=state.generation;await request(`/ui/v1/runs/${encodeURIComponent(id)}/save`,{method:'POST'});
  const data=await request(`/ui/v1/runs/${encodeURIComponent(id)}`),ctx=ensureContext(id,data);ctx.rendered=false;
  if(state.generation===generation)renderRun(ctx);archive.refreshIfVisible();
}

function reviewState(id){let value=reviews.get(id);if(!value){const run=runs.get(id)?.data||{};value={last_playback_sec:finite(run.last_playback_sec),review_status:run.review_status||'not_reviewed',reviewed_event_ids:[...(run.reviewed_event_ids||[])]};reviews.set(id,value);}return value;}
function recordPosition(position){const id=state.displayRunId;if(!id||!player.url||!player.available)return;const review=reviewState(id);review.last_playback_sec=Math.max(0,finite(position));queueReview();}
function markEvent(id){const runId=state.displayRunId;if(!runId)return;const review=reviewState(runId);if(!review.reviewed_event_ids.includes(id))review.reviewed_event_ids.push(id);if(review.review_status==='not_reviewed')review.review_status='in_review';chat.setReviewed(review.reviewed_event_ids);report.review.value=review.review_status;queueReview();}
function updateReview(status){if(!state.displayRunId)return;reviewState(state.displayRunId).review_status=status;queueReview();}
function queueReview(){clearTimeout(reviewTimer);reviewTimer=setTimeout(()=>flushReview(),1200);}
function flushReview(){
  clearTimeout(reviewTimer);const id=state.displayRunId;if(!id||!reviews.has(id)||runs.get(id)?.deleted)return;
  const snapshot={...reviewState(id),reviewed_event_ids:[...reviewState(id).reviewed_event_ids]};
  request(`/ui/v1/runs/${encodeURIComponent(id)}/review`,{method:'PATCH',body:snapshot,keepalive:true}).then(()=>archive.refreshIfVisible()).catch(error=>{if(state.displayRunId===id)showError(`Не удалось сохранить позицию просмотра: ${error.message}`);});
}
window.addEventListener('pagehide',()=>flushReview());

for(const id of ['info-button','nav-help'])$(id)?.addEventListener('click',()=>showHelp(request,state.caps).catch(showError));
document.addEventListener('click',event=>{const close=event.target.closest('[data-close]');if(close)close.closest('dialog')?.close();if(event.target.closest('button:not(#sound-toggle)'))audio.play('click');});
for(const dialog of document.querySelectorAll('dialog'))dialog.addEventListener('click',event=>{if(event.target===dialog){const rect=dialog.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)dialog.close();}});

async function restoreActiveRuns(){
  for(const status of ['queued','processing']){
    let offset=0,total=1;
    while(offset<total){const page=await request(`/ui/v1/runs?status=${status}&limit=100&offset=${offset}`);total=finite(page.total);for(const item of page.items||[]){const id=String(item.job_id||item.id),ctx=ensureContext(id,item);poll(ctx);}offset+=100;}
  }
}
async function afterLogin(target){
  if(restoring)return;restoring=true;
  try{
    const next=route(),id=next.runId||storedRun();
    restoreActiveRuns().catch(error=>showError(`Не удалось восстановить фоновые задания: ${error.message}`));
    if(id){try{await openRun(id,{navigateTo:false});}catch(error){rememberRun(null);showError(error);}}
    await navigate(target||next.world||'overworld',false);
  }finally{restoring=false;updateControls();}
}
async function boot(){
  chat.clear();updateControls();$('typed-message').textContent=CONFIG.greeting;
  const initial=route();
  try{const info=await request('/ui/v1/session');setAuthenticated(true);applyCapabilities(info);await afterLogin(initial.world==='credits'?'end':initial.world);}
  catch(error){if(error.status!==401){$('welcome-error').textContent=error.message;$('welcome-error').hidden=false;}state.authTarget=initial.world==='overworld'?'nether':initial.world;await navigate('overworld',false);}
}
boot().catch(error=>{showError(error);$('welcome-error').textContent=error.message;$('welcome-error').hidden=false;});
