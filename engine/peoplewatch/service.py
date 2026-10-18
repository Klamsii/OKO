"""Authenticated asynchronous video backend. Run behind the team's TLS proxy."""
import argparse
from contextlib import asynccontextmanager
import hmac
import json
import os
from pathlib import Path
import uuid
from typing import Literal

from fastapi import Depends,FastAPI,File,Header,HTTPException,UploadFile
from pydantic import BaseModel,Field
from .service_jobs import JobService,model_factory


class Accepted(BaseModel):
    job_id: str


class EvidenceFrame(BaseModel):
    timestamp_sec: float=Field(ge=0)
    image_base64: str=Field(min_length=1,description='Original frame encoded as JPEG, standard base64')


class Violation(BaseModel):
    type: str
    timecode_sec: float=Field(ge=0)
    description: str
    evidence_frames: list[EvidenceFrame]=Field(min_length=1,max_length=3)


class Processing(BaseModel):
    job_id: str
    status: Literal['processing']


class Done(BaseModel):
    job_id: str
    status: Literal['done']
    verdict: Literal['violation','no_violation']
    violations: list[Violation]
    assessment_mode: Literal['strict','best_effort']='strict'
    preliminary: bool=False
    decision_summary: str=''
    unresolved_questions: int=0


class Failed(BaseModel):
    job_id: str
    status: Literal['failed']
    error: dict[str,str]


class DetailedReport(BaseModel):
    job_id: str
    status: Literal['processing','done','failed']
    analysis_status: str
    verdict: str
    scope: str
    violations: list[dict]
    review_issues: list[dict]
    abstention_reasons: list[dict]
    stage_seconds: dict[str,float]
    elapsed_seconds: float|None
    assessment_mode: Literal['strict','best_effort']='strict'
    preliminary: bool=False
    decision_summary: str=''
    strict_rule_verdict: str|None=None
    working_assumptions: list[dict]=[]
    possible_violations: list[dict]=[]
    moment_assessments: list[dict]=[]
    optional_review_failures: list[dict]=[]


def create_app(root,api_key,config=None,factory=model_factory,deadline_seconds=300,
               max_upload_bytes=256*1024*1024):
    if not api_key: raise ValueError('DATATHON_API_KEY is required')
    root=Path(root).resolve(); service=JobService(root,factory,config,deadline_seconds)
    @asynccontextmanager
    async def lifespan(app):
        service.start()
        try: yield
        finally: service.close()
    app=FastAPI(title='Datathon Video Analysis',version='1.0.0',lifespan=lifespan,
                docs_url=None,redoc_url=None,openapi_url=None)
    app.state.jobs=service
    async def authenticate(x_api_key: str|None=Header(default=None,alias='X-API-Key')):
        if x_api_key is None or not hmac.compare_digest(x_api_key.encode(),api_key.encode()):
            raise HTTPException(401,'invalid_api_key')

    @app.get('/health',dependencies=[Depends(authenticate)])
    async def health(): return {'ready':service.ready}

    @app.get('/api/v1/openapi.json',dependencies=[Depends(authenticate)])
    async def schema(): return app.openapi()

    @app.post('/api/v1/analyze',status_code=202,response_model=Accepted,dependencies=[Depends(authenticate)])
    async def analyze(file: UploadFile=File(...)):
        if Path(file.filename or '').suffix.lower()!='.mov':
            await file.close(); raise HTTPException(415,'expected_mov_video')
        if not service.ready:
            await file.close(); raise HTTPException(503,'model_not_ready')
        job_id=uuid.uuid4().hex; directory=root/job_id; directory.mkdir()
        source=directory/'source.mov'; size=0
        try:
            with source.open('xb') as stream:
                while chunk:=await file.read(1024*1024):
                    size+=len(chunk)
                    if size>max_upload_bytes: raise HTTPException(413,'video_too_large')
                    stream.write(chunk)
            if not size: raise HTTPException(400,'empty_video')
            try: service.submit(job_id,source)
            except RuntimeError as exc: raise HTTPException(503,str(exc)) from exc
        except BaseException:
            # Only our unsubmitted upload is removed; accepted jobs retain all artifacts.
            if not (directory/'state.json').exists():
                source.unlink(missing_ok=True); directory.rmdir()
            raise
        finally: await file.close()
        return {'job_id':job_id}

    @app.get('/api/v1/jobs/{job_id}',response_model=Processing|Done|Failed,dependencies=[Depends(authenticate)])
    async def result(job_id: str):
        try: return service.get(job_id)
        except KeyError: raise HTTPException(404,'job_not_found')

    @app.get('/api/v1/jobs/{job_id}/report',response_model=DetailedReport,dependencies=[Depends(authenticate)])
    async def detailed_report(job_id: str):
        try: state=service.get(job_id)
        except KeyError: raise HTTPException(404,'job_not_found')
        path=root/job_id/'report.json'
        if not path.is_file(): raise HTTPException(409,'report_not_ready')
        report=json.loads(path.read_text('utf-8'))
        fields=('type','description','event_start_seconds','key_moment_seconds','event_end_seconds',
                'timeline_status','normative_basis','certainty','requires_review','affects_verdict','evidence_level')
        # External callers receive useful diagnostics, never server filesystem
        # paths, source videos, prompts or raw model output through this route.
        return dict(job_id=job_id,status=state['status'],analysis_status=report.get('Status','unknown'),
            verdict=report.get('Verdict','unknown'),scope=report.get('scope',''),
            violations=[{k:v[k] for k in fields if k in v} for v in report.get('violations',[])],
            review_issues=report.get('review_issues',[]),abstention_reasons=report.get('abstention_reasons',[]),
            stage_seconds=report.get('stage_seconds',{}),elapsed_seconds=report.get('elapsed_seconds'),
            assessment_mode=report.get('assessment_mode','strict'),preliminary=report.get('preliminary',False),
            decision_summary=report.get('decision_summary',''),strict_rule_verdict=report.get('strict_rule_verdict'),
            working_assumptions=report.get('working_assumptions',[]),
            possible_violations=[{k:v[k] for k in fields if k in v} for v in report.get('possible_violations',[])],
            moment_assessments=report.get('moment_assessments',[]),
            optional_review_failures=[dict(question=r['question'],anchor_seconds=r['anchor_seconds'],
                error=r['parse_error'],retry_attempted='retry_raw' in r)
                for r in report.get('visual_episode_reviews',[]) if r.get('parse_error')])

    return app


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--root',type=Path,required=True)
    p.add_argument('--model',type=Path,required=True)
    p.add_argument('--yolo',type=Path,required=True)
    p.add_argument('--pose',type=Path)
    p.add_argument('--adapter',type=Path)
    p.add_argument('--decision-mode',choices=['strict','best_effort'],default='best_effort')
    p.add_argument('--host',default='127.0.0.1')
    p.add_argument('--port',type=int,default=8000)
    args=p.parse_args()
    key=os.environ.get('DATATHON_API_KEY')
    if not key: p.error('Set DATATHON_API_KEY in the service environment')
    config={k:str(v) for k,v in vars(args).items() if k in ('model','yolo','pose','adapter','decision_mode') and v is not None}
    import uvicorn
    # Exactly one service supervisor for this job root. GPU concurrency is
    # explicit, never accidental uvicorn --workers replication of model weights.
    uvicorn.run(create_app(args.root,key,config),host=args.host,port=args.port)


if __name__=='__main__': main()
