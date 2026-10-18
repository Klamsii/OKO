export function formatTime(value, fractional=false){
  if(!Number.isFinite(value)||value<0)return '00:00';
  const milliseconds=Math.round(value*1000),sec=fractional?Math.floor(milliseconds/1000):Math.floor(value),hours=Math.floor(sec/3600),minutes=Math.floor(sec/60)%60;
  const stamp=`${String(minutes).padStart(2,'0')}:${String(sec%60).padStart(2,'0')}`;
  return (hours?`${hours}:`:'')+stamp+(fractional?`.${String(milliseconds%1000).padStart(3,'0')}`:'');
}

export class VideoPlayer{
  constructor({audio,onError,onPosition=()=>{}}){
    this.video=document.getElementById('video');this.container=document.getElementById('player-container');this.audio=audio;this.onError=onError;
    this.playButton=document.getElementById('play-button');this.muteButton=document.getElementById('video-mute');this.zoomButton=document.getElementById('zoom-button');
    this.progress=document.getElementById('video-progress');this.zoom=1;this.pendingSeek=null;this.objectUrl=null;this.available=false;this.onPosition=onPosition;this.positionListeners=new Set();
    this.playButton.addEventListener('click',()=>this.togglePlay());this.video.addEventListener('click',()=>this.togglePlay());
    this.muteButton.addEventListener('click',()=>this.toggleMute());this.zoomButton.addEventListener('click',()=>this.toggleZoom());
    document.getElementById('fullscreen-button').addEventListener('click',()=>this.fullscreen());
    this.progress.addEventListener('input',()=>this.seek(Number(this.progress.value)));
    this.video.addEventListener('loadedmetadata',()=>this.metadata());
    this.video.addEventListener('timeupdate',()=>{this.updateTime();this.emitPosition();});
    this.video.addEventListener('play',()=>{this.updatePlay();this.audio.duck(true);});
    this.video.addEventListener('pause',()=>{this.updatePlay();this.audio.duck(false);this.emitPosition();});
    this.video.addEventListener('ended',()=>{this.updatePlay();this.audio.duck(false);this.emitPosition();});
    this.video.addEventListener('volumechange',()=>this.updateMute());
    this.video.addEventListener('error',()=>{
      if(!this.url)return;
      this.available=false;document.getElementById('playback-error').hidden=false;
      this.setControls(false);this.onError('Браузер не воспроизводит этот кодек. Анализ и отчёт доступны; используйте кадры и таймлайн.');
    });
    document.addEventListener('keydown',event=>{
      if(document.getElementById('workspace-scene').hidden||!this.url||event.ctrlKey||event.altKey||event.metaKey||event.target.closest('input,textarea,select,dialog,[contenteditable="true"]'))return;
      if(event.code==='Space'&&!event.target.closest('button')){event.preventDefault();this.togglePlay();}
      if(event.key==='ArrowLeft'){event.preventDefault();this.seek(this.video.currentTime-5);}
      if(event.key==='ArrowRight'){event.preventDefault();this.seek(this.video.currentTime+5);}
      if(event.key.toLowerCase()==='z')this.toggleZoom();
      if(event.key.toLowerCase()==='m')this.toggleMute();
      if(event.key.toLowerCase()==='f')this.fullscreen();
    });
    window.addEventListener('pagehide',event=>{if(!event.persisted&&this.objectUrl)URL.revokeObjectURL(this.objectUrl);});
  }
  setControls(enabled){for(const control of this.container.querySelectorAll('button,input'))control.disabled=!enabled;}
  select(file){
    this.loadSource(URL.createObjectURL(file),{objectUrl:true});
  }
  loadSource(url,{position=0,objectUrl=false}={}){
    this.clear();this.url=url;this.objectUrl=objectUrl?url:null;this.video.src=url;this.video.hidden=false;this.pendingSeek=position;
    document.getElementById('empty-state').hidden=true;document.getElementById('playback-error').hidden=true;
    document.getElementById('video-resolution').textContent='ЗАГРУЗКА';this.setControls(false);this.updateTime();this.video.load();
  }
  clear(){
    this.pause();this.url=null;this.available=false;this.video.removeAttribute('src');this.video.load();this.video.hidden=true;
    if(this.objectUrl)URL.revokeObjectURL(this.objectUrl);this.objectUrl=null;this.pendingSeek=null;this.zoom=1;this.updateZoom();this.setControls(false);
    this.progress.max='0';this.progress.value='0';document.getElementById('current-time').textContent='00:00';document.getElementById('duration').textContent='00:00';
    document.getElementById('video-resolution').textContent='— × —';document.getElementById('empty-state').hidden=false;document.getElementById('playback-error').hidden=true;
  }
  subscribePosition(fn){this.positionListeners.add(fn);return()=>this.positionListeners.delete(fn);}
  emitPosition(){const position=Number(this.video.currentTime)||0;this.onPosition(position);for(const fn of this.positionListeners)fn(position);}
  metadata(){
    if(!this.url||!Number.isFinite(this.video.duration))return;this.available=true;
    this.progress.max=String(this.video.duration);document.getElementById('duration').textContent=formatTime(this.video.duration);
    document.getElementById('video-resolution').textContent=`${this.video.videoWidth} × ${this.video.videoHeight}`;
    this.setControls(true);if(this.pendingSeek!==null){const t=this.pendingSeek;this.pendingSeek=null;this.seek(t);}this.updateTime();
  }
  updateTime(){document.getElementById('current-time').textContent=formatTime(this.video.currentTime);this.progress.value=String(this.video.currentTime||0);this.progress.setAttribute('aria-valuetext',formatTime(this.video.currentTime));}
  updatePlay(){const paused=this.video.paused;this.playButton.querySelector('use').setAttribute('href',paused?'#i-play':'#i-pause');this.playButton.setAttribute('aria-label',paused?'Воспроизвести':'Пауза');this.playButton.title=paused?'Воспроизвести (Пробел)':'Пауза (Пробел)';}
  async togglePlay(){if(!this.url||this.playButton.disabled)return;if(this.video.paused){try{await this.video.play();}catch{this.onError('Воспроизведение недоступно. Анализ можно запустить отдельно.');}}else this.pause();}
  pause(){this.video.pause();}
  toggleMute(){this.video.muted=!this.video.muted;}
  updateMute(){this.muteButton.querySelector('use').setAttribute('href',this.video.muted?'#i-muted':'#i-sound');this.muteButton.setAttribute('aria-label',this.video.muted?'Включить звук видео':'Выключить звук видео');}
  seek(second){
    if(!Number.isFinite(second))return;
    if(!this.video.readyState||!Number.isFinite(this.video.duration)){this.pendingSeek=second;return;}
    this.video.currentTime=Math.max(0,Math.min(second,this.video.duration));this.updateTime();this.emitPosition();
  }
  toggleZoom(){if(!this.url||this.zoomButton.disabled)return;this.zoom=this.zoom===1?2:1;this.updateZoom();}
  updateZoom(){this.video.classList.toggle('zoomed',this.zoom===2);this.zoomButton.querySelector('span').textContent=`${this.zoom}×`;this.zoomButton.setAttribute('aria-label',this.zoom===1?'Увеличить видео до 2×':'Вернуть масштаб 1×');document.getElementById('zoom-badge').hidden=this.zoom!==2;}
  async fullscreen(){try{if(document.fullscreenElement)await document.exitFullscreen();else if(this.container.requestFullscreen)await this.container.requestFullscreen();else this.onError('Полноэкранный режим не поддерживается этим браузером.');}catch{this.onError('Браузер не разрешил полноэкранный режим.');}}
}
