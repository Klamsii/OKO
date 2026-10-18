"""Durable job states and one warm, supervised model process per service."""
from collections import deque
import json
import multiprocessing
from pathlib import Path
from queue import Empty
import re
import threading
import time
import traceback

from .preparation_jobs import _write


def _worker(factory,config,inbox,outbox):
    try:
        runner=factory(config)
        outbox.put(('ready',None,None))
    except Exception:
        traceback.print_exc(); outbox.put(('init_failed',None,None)); return
    while True:
        job=inbox.get()
        if job is None: return
        job_id,source,directory=job
        try:
            result=runner(job_id,Path(source),Path(directory))
            outbox.put(('result',job_id,result))
        except Exception as exc:
            from .api_result import UnresolvedResult
            code=str(exc) if isinstance(exc,UnresolvedResult) else 'analysis_failed'
            traceback.print_exc(); outbox.put(('error',job_id,code))


class JobService:
    def __init__(self,root,factory,config=None,deadline_seconds=300,max_pending=2):
        self.root=Path(root).resolve(); self.root.mkdir(parents=True,exist_ok=True)
        self.factory=factory; self.config=config or {}; self.deadline_seconds=deadline_seconds
        self.max_pending=max_pending; self.pending=deque(); self.active=None; self.ready=False
        self.lock=threading.RLock(); self.stop=threading.Event(); self.process=None
        self.context=multiprocessing.get_context('spawn'); self.deadlines={}; self.accepted={}
        # A previous process cannot be assumed still running after service restart.
        for path in self.root.glob('*/state.json'):
            state=json.loads(path.read_text('utf-8'))
            if state.get('status')=='processing':
                _write(path,dict(job_id=state['job_id'],status='failed',error={'code':'service_restarted'}))

    def start(self):
        with self.lock: self._spawn()
        self.thread=threading.Thread(target=self._monitor,name='video-job-supervisor',daemon=True)
        self.thread.start()

    def _spawn(self):
        self.ready=False
        self.worker_initialized=False
        self.inbox=self.context.Queue(); self.outbox=self.context.Queue()
        self.process=self.context.Process(target=_worker,args=(self.factory,self.config,self.inbox,self.outbox),daemon=True)
        self.process.start()

    def _kill(self):
        self.ready=False
        if self.process is not None:
            if self.process.is_alive(): self.process.terminate()
            self.process.join(timeout=1)
            if self.process.is_alive(): self.process.kill(); self.process.join(timeout=1)
        for channel in (self.inbox,self.outbox):
            channel.cancel_join_thread(); channel.close()

    def _path(self,job_id):
        if not re.fullmatch('[0-9a-f]{32}',job_id): raise KeyError(job_id)
        return self.root/job_id/'state.json'

    def submit(self,job_id,source):
        with self.lock:
            if not self.ready: raise RuntimeError('model_not_ready')
            if len(self.pending)>=self.max_pending: raise RuntimeError('queue_full')
            path=self._path(job_id)
            if path.exists(): raise ValueError('Duplicate job')
            now=time.monotonic(); self.accepted[job_id]=now
            self.deadlines[job_id]=now+self.deadline_seconds
            _write(path,dict(job_id=job_id,status='processing'))
            self.pending.append((job_id,str(source),str(path.parent)))

    def get(self,job_id):
        with self.lock:
            path=self._path(job_id)
            if not path.exists(): raise KeyError(job_id)
            return json.loads(path.read_text('utf-8'))

    def _finish(self,job_id,result):
        _write(self._path(job_id),result)
        started=self.accepted.pop(job_id,None)
        _write(self._path(job_id).parent/'service_timing.json',dict(
            processing_seconds=time.monotonic()-started if started is not None else None,
            includes_queue=True,deadline_seconds=self.deadline_seconds))
        self.deadlines.pop(job_id,None)

    def _fail(self,job_id,code):
        self._finish(job_id,dict(job_id=job_id,status='failed',error={'code':code}))

    def _tick(self):
        now=time.monotonic()
        # Expiration is checked before accepting an arriving result. A late
        # inference cannot overwrite the terminal timeout with a successful result.
        if self.active and now>=self.deadlines[self.active]:
            self._fail(self.active,'deadline_exceeded'); self.active=None
            self._kill(); self._spawn()
        remaining=deque()
        for job in self.pending:
            if now>=self.deadlines[job[0]]: self._fail(job[0],'deadline_exceeded')
            else: remaining.append(job)
        self.pending=remaining
        while True:
            try: kind,job_id,payload=self.outbox.get_nowait()
            except Empty: break
            if kind=='ready': self.ready=True; self.worker_initialized=True
            elif kind=='init_failed': self.ready=False
            elif job_id==self.active and self.active is not None:
                if kind=='result':
                    if payload.get('job_id')!=job_id or payload.get('status')!='done':
                        self._fail(job_id,'invalid_worker_result')
                    else: self._finish(job_id,payload)
                else: self._fail(job_id,payload)
                self.active=None
        if self.process and not self.process.is_alive():
            self.ready=False
            if self.active:
                self._fail(self.active,'worker_crashed'); self.active=None
            # Initialization failure is exposed by readiness; do not repeatedly
            # allocate a GPU with missing weights. A service restart retries it.
            for job in self.pending: self._fail(job[0],'worker_unavailable')
            self.pending.clear()
            if self.worker_initialized:
                self._kill(); self._spawn()
        if self.ready and self.active is None and self.pending:
            job=self.pending.popleft(); self.active=job[0]; self.inbox.put(job)

    def _monitor(self):
        while not self.stop.wait(.1):
            with self.lock:
                try: self._tick()
                except Exception:
                    traceback.print_exc(); self.ready=False
                    if self.active:
                        self._fail(self.active,'supervisor_failed'); self.active=None
                    for job in self.pending: self._fail(job[0],'supervisor_failed')
                    self.pending.clear(); self._kill(); return

    def close(self):
        self.stop.set()
        if hasattr(self,'thread'): self.thread.join(timeout=3)
        with self.lock:
            if self.active: self._fail(self.active,'service_stopped'); self.active=None
            for job in self.pending: self._fail(job[0],'service_stopped')
            self.pending.clear()
            if self.process is not None: self._kill()


def model_factory(config):
    from cloud.model import H100Detector
    from .hybrid import HybridDetector
    from .hand_focus import PoseHandCropper
    from .video import prepare
    from .action_pipeline import run_actions
    from .api_result import make_result
    model=H100Detector(Path(config['model']),Path(config['adapter']) if config.get('adapter') else None)
    detector=HybridDetector(Path(config['yolo']),model)
    pose=PoseHandCropper(Path(config['pose'])) if config.get('pose') else None
    def run(job_id,source,directory):
        start=time.perf_counter()
        prepared,video=prepare(source,directory/'analysis')
        prepared_at=time.perf_counter()
        report=run_actions(model,video,prepared,detector,pose_cropper=pose,
                           decision_mode=config.get('decision_mode','best_effort'))
        report['video_prepare_seconds']=prepared_at-start
        report['test_wall_seconds']=time.perf_counter()-start
        report['model_metadata']=model.metadata
        _write(directory/'report.json',report)
        rows=json.loads((Path(report['output'])/'observations.json').read_text('utf-8'))
        return make_result(job_id,report,rows,[directory])
    return run
