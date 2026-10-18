"""Opt-in real GPU integration. Run on the team's server, never in normal CI.

Starts private loopback services, submits one ORIGINAL video, writes a compact
audit without images/keys, and stops only its own services in finally.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time

import httpx


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--video',type=Path,required=True);p.add_argument('--model',required=True)
    p.add_argument('--yolo',required=True);p.add_argument('--pose',required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--pause-baseline',type=int)
    a=p.parse_args(); root=Path(__file__).resolve().parents[1]
    a.output.mkdir(parents=True,exist_ok=True)
    key=secrets.token_urlsafe(32);children=[];logs=[];paused=False
    env={**os.environ,'API_KEY':key,'DATATHON_API_KEY':key,'DATATHON_API_URL':'http://127.0.0.1:8766',
         'ANALYSIS_BACKEND':'datathon','DATA_DIR':str(a.output/'site'),'EMBEDDED_WORKER':'1',
         'OKO_DEADLINE_SEC':'300','OKO_MAX_QUEUE':'2','PYTHONUNBUFFERED':'1'}
    proof={'test':'original video through website API and real Qwen GPU','accuracy_test':False}
    def spawn(args,cwd,label):
        log=(a.output/(label+'.log')).open('wb');logs.append(log)
        process=subprocess.Popen([sys.executable,*args],cwd=cwd,env=env,stdout=log,stderr=log,start_new_session=True)
        children.append(process)
    try:
        if a.pause_baseline:
            cmdline=Path(f'/proc/{a.pause_baseline}/cmdline').read_bytes()
            if b'cloud.run_inference' not in cmdline: raise RuntimeError('Unrecognized baseline process; not paused')
            os.kill(a.pause_baseline,signal.SIGSTOP);paused=True
        spawn(['-m','peoplewatch.service','--root',str(a.output/'engine'),'--model',a.model,'--yolo',a.yolo,
               '--pose',a.pose,'--port','8766','--decision-mode','best_effort'],root/'engine','model')
        spawn(['-m','app','--port','8046'],root,'website')
        with httpx.Client(headers={'X-API-Key':key},timeout=20,trust_env=False) as client:
            ready_until=time.monotonic()+180
            while time.monotonic()<ready_until:
                try:
                    model=client.get('http://127.0.0.1:8766/health')
                    site=client.get('http://127.0.0.1:8046/health')
                    if model.status_code==200 and model.json().get('ready') and site.status_code==200: break
                except httpx.HTTPError: pass
                time.sleep(2)
            else: raise RuntimeError('Services did not become ready; see private logs')
            print('READY: website + real Qwen',flush=True)
            started=time.monotonic()
            with a.video.open('rb') as stream:
                response=client.post('http://127.0.0.1:8046/api/v1/analyze',files={'file':(a.video.name,stream,'video/quicktime')})
            response.raise_for_status();job_id=response.json()['job_id']
            proof['site_job_id']=job_id
            print('ACCEPTED',job_id,flush=True)
            while time.monotonic()-started < 315:
                response=client.get(f'http://127.0.0.1:8046/api/v1/jobs/{job_id}');response.raise_for_status();state=response.json()
                if state['status']!='processing': break
                time.sleep(2)
            proof.update(status=state['status'],verdict=state.get('verdict'),wall_seconds=round(time.monotonic()-started,3),
                         violations=len(state.get('violations',[])))
            run=client.get(f'http://127.0.0.1:8046/ui/v1/runs/{job_id}').json()
            result=run.get('result') or {}
            proof.update(preliminary=result.get('preliminary'),analysis_status=result.get('analysis_status'),
                         decision_summary=result.get('decision_summary'),model_job_id=result.get('model_job_id'),
                         moments=len(result.get('moment_assessments',[])),issues=len(result.get('review_issues',[])),
                         optional_review_failures=len(result.get('optional_review_failures',[])),error=state.get('error'))
            (a.output/'site-result.json').write_text(json.dumps(run,ensure_ascii=False),encoding='utf-8')
            assert state['status']=='done', f"Analysis failed: {state.get('error')}"
            assert proof['wall_seconds']<=300, 'End-to-end deadline missed'
            assert result['analysis_scope']['mode']=='datathon'
            assert client.get(f'http://127.0.0.1:8046/api/v1/jobs/{job_id}',headers={'X-API-Key':'wrong'}).status_code==401
            proof['checks_passed']=True
    except Exception as exc:
        proof['checks_passed']=False;proof['error']=str(exc)[:500]
        raise
    finally:
        for process in reversed(children):
            if process.poll() is None:
                os.killpg(process.pid,signal.SIGTERM)
                try: process.wait(10)
                except subprocess.TimeoutExpired: os.killpg(process.pid,signal.SIGKILL);process.wait()
        for log in logs:log.close()
        if paused:
            try:os.kill(a.pause_baseline,signal.SIGCONT)
            except ProcessLookupError:pass
        (a.output/'audit.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(proof,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
