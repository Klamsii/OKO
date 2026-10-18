"""HTTP/storage/subprocess integration using isolated temporary archives.

Synthetic JSON fixtures below test contracts only. The parametrized video test
executes the real local obstruction detector and verifies actual JPEG bytes.
"""
import base64
import hashlib
import hmac
import io
import json
import time
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import db
from app.config import Settings
from app.worker import process_integrated_job

KEY = 'isolated-integration-key-0123456789'
VIDEO_BYTES = b'\x00\x00\x00\x18ftypqt  ' + b'fixture-only-not-a-decodable-video' * 3


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv('API_KEY', KEY)
    monkeypatch.setenv('EMBEDDED_WORKER', '0')
    from app import api
    settings = Settings(
        data_dir=tmp_path / 'private', rules_path=Path(__file__).resolve().parents[1] / 'rules.json',
        api_key=KEY, max_upload_bytes=4 * 1024 * 1024, max_video_seconds=30,
        model_timeout_seconds=15, retention_hours=24, worker_concurrency=1,
        model_command=(), ffmpeg='ffmpeg', ffprobe='ffprobe', deadline_seconds=25,
        reserve_seconds=2, embedded_worker=False, face_blur='auto')
    monkeypatch.setattr(api, 'settings', settings)
    with TestClient(api.app) as client:
        yield api, client, settings


def login(client):
    response = client.post('/ui/v1/session', json={'api_key': KEY}, headers={'Origin': 'http://testserver'})
    assert response.status_code == 200
    return response


def test_public_ui_auto_session_keeps_direct_api_key_private(service, monkeypatch):
    api, client, settings = service
    monkeypatch.setattr(api, 'settings', replace(settings, public_ui_access=True))
    response = client.get('/ui/v1/session')
    assert response.status_code == 200
    assert 'httponly' in response.headers['set-cookie'].lower()
    assert KEY not in response.text
    assert client.get('/ui/v1/runs').status_code == 200
    assert client.get('/api/v1/capabilities').status_code == 401
    assert client.post('/ui/v1/session', json={'api_key': 'invalid'}).status_code == 401


def submit(client, *, key='request-one', filename='clip.mov', payload=VIDEO_BYTES, ui=True):
    headers = {'Idempotency-Key': key} if key else {}
    if not ui:
        headers['X-API-Key'] = KEY
    return client.post('/ui/v1/analyze' if ui else '/api/v1/analyze',
                       files={'file': (filename, payload, 'video/quicktime')}, headers=headers)


def fixture_reports(run_id):
    api = {'job_id': run_id, 'status': 'done', 'verdict': 'violation', 'violations': [
        {'type': 'fixture event', 'timecode_sec': 1.0, 'description': 'contract fixture',
         'evidence_frames': [], 'rule': {'extra': 'not jury schema'}}]}
    ui = {'verdict': 'violation', 'violations': [{'rule': {'id': 'V13'}, 'timecode': 1.0}],
          'review': [], 'video': {'duration_sec': 5.0}, 'notes': ['fixture saved without model'],
          'activity': [0, 1], 'thumbs': [], 'degraded': False}
    return api, ui


def test_frontend_resource_versions_change_with_local_edits(service, tmp_path, monkeypatch):
    api, client, _ = service
    frontend = tmp_path / 'frontend'
    frontend.mkdir()
    (frontend / 'index.html').write_text(
        '<link href="/static/styles.css"><script src="/static/app.js"></script>', encoding='utf-8')
    style = frontend / 'styles.css'
    style.write_text('body{color:green}', encoding='utf-8')
    monkeypatch.setattr(api, 'web_dir', frontend)
    first = client.get('/')
    version = first.headers['x-ui-version']
    assert first.headers['cache-control'] == 'no-store'
    assert f'/_ui/{version}/styles.css' in first.text
    assert f'/_ui/{version}/app.js' in first.text
    assert client.get('/').headers['x-ui-version'] == version
    style.write_text('body{color:purple;background:black}', encoding='utf-8')
    assert client.get('/').headers['x-ui-version'] != version


def test_private_session_lifecycle_origin_and_expiry(service):
    api, client, settings = service
    assert client.get('/ui/v1/session').status_code == 401
    assert client.get('/ui/v1/runs').status_code == 401
    assert client.get('/ui/v1/catalog').status_code == 401
    assert client.get('/api/v1/capabilities').status_code == 401
    assert client.post('/ui/v1/session', json={'api_key': 'invalid'}).status_code == 401
    assert client.post('/ui/v1/session', json={'api_key': KEY},
                       headers={'Origin': 'https://unrelated.example'}).status_code == 403
    response = login(client)
    cookie = response.headers['set-cookie'].lower()
    assert 'httponly' in cookie and 'samesite=strict' in cookie
    assert client.get('/ui/v1/session').json()['archive_scope'] == 'installation'
    saved_cookie = client.cookies.get(api.COOKIE)
    client.cookies.clear()
    assert client.get('/ui/v1/session').status_code == 401
    client.cookies.set(api.COOKIE, saved_cookie, domain='testserver.local', path='/')
    assert client.get('/ui/v1/session').status_code == 200
    assert client.delete('/ui/v1/session', headers={'Origin': 'http://unrelated.example'}).status_code == 403
    assert client.delete('/ui/v1/session', headers={'Origin': 'http://testserver'}).status_code == 200
    assert client.get('/ui/v1/session').status_code == 401
    encoded = base64.urlsafe_b64encode(f'{int(time.time()) - settings.session_hours * 3600 - 60}:expired'.encode()).decode().rstrip('=')
    signature = hmac.new(KEY.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    client.cookies.set(api.COOKIE, encoded + '.' + signature, domain='testserver.local', path='/')
    assert client.get('/ui/v1/session').status_code == 401
    client.cookies.set(api.COOKIE, 'malformed.invalid', domain='testserver.local', path='/')
    assert client.get('/ui/v1/session').status_code == 401


def test_https_origin_behind_tls_proxy_keeps_csrf_protection(service, monkeypatch):
    _, client, _ = service
    monkeypatch.setenv('OKO_PUBLIC_ORIGIN', 'https://testserver')
    response = client.post('/ui/v1/session', json={'api_key': KEY},
                           headers={'Origin': 'https://testserver'})
    assert response.status_code == 200
    assert 'secure' in response.headers['set-cookie'].lower()
    assert client.post('/ui/v1/session', json={'api_key': KEY},
                       headers={'Origin': 'http://testserver'}).status_code == 403
    assert client.post('/ui/v1/session', json={'api_key': KEY},
                       headers={'Origin': 'https://other.example'}).status_code == 403
    assert client.delete('/ui/v1/session', headers={'Origin': 'https://testserver',
                                                   'X-API-Key': KEY}).status_code == 200


def test_capabilities_and_all_documented_upload_containers(service):
    _, client, _ = service
    login(client)
    caps = client.get('/api/v1/capabilities', headers={'X-API-Key': KEY})
    assert caps.status_code == 200
    assert set(caps.json()['formats']) == {'.mov', '.qt', '.mp4', '.m4v', '.avi', '.mkv', '.webm'}
    assert caps.json()['detectors'] == ['obstruction']
    scope = caps.json()['analysis_scope']
    assert scope['mode'] == 'camera_visibility'
    assert scope['checked_rule_ids'] == ['V13']
    assert scope['team_model']['available'] is False
    assert scope['full_audit_available'] is False
    for extension in caps.json()['formats']:
        if extension == '.avi':
            payload = b'RIFF\x00\x00\x00\x00AVI ' + b'fixture' * 3
        elif extension in ('.mkv', '.webm'):
            payload = b'\x1a\x45\xdf\xa3' + b'fixture' * 3
        else:
            payload = VIDEO_BYTES
        response = submit(client, key='format-' + extension, filename='file' + extension, payload=payload)
        assert response.status_code == 200, (extension, response.text)
    assert client.post('/ui/v1/analyze').status_code == 400
    assert submit(client, filename='clip.txt', key='invalid-extension').status_code == 415
    assert submit(client, payload=b'not a video file', key='invalid-header').status_code == 415
    assert submit(client, payload=b'', key='empty').status_code == 400
    assert client.get('/healthz').status_code == 200
    catalog = client.get('/ui/v1/catalog').json()
    assert len(catalog['rules']) == 21
    assert [rule['id'] for rule in catalog['rules'] if rule['local_check_available']] == ['V13']


def test_archived_scope_does_not_change_when_a_model_is_later_enabled(service, monkeypatch):
    api, client, settings = service
    login(client)
    job_id = submit(client).json()['job_id']
    db.complete_run(settings.data_dir, job_id, *fixture_reports(job_id))
    monkeypatch.setattr(api, 'settings', replace(settings, enable_team_model=True,
                                               model_command=('private-model-command',)))
    current = client.get('/ui/v1/session').json()['analysis_scope']
    assert current['team_model']['enabled'] is True
    assert current['team_model']['available'] is None
    assert current['team_model']['status'] == 'configured_unverified'
    assert current['rule_coverage_known'] is False
    assert 'private-model-command' not in json.dumps(current)
    old = client.get(f'/ui/v1/runs/{job_id}').json()['analysis_scope']
    assert old['mode'] == 'camera_visibility'
    assert old['checked_rule_ids'] == ['V13']
    assert old['team_model']['enabled'] is False
    assert client.get('/ui/v1/runs').json()['items'][0]['analysis_scope'] == old


def test_legacy_report_does_not_invent_detector_coverage(service, tmp_path):
    _, client, settings = service
    login(client)
    file = tmp_path / 'legacy.mov'
    file.write_bytes(VIDEO_BYTES)
    job_id = db.create_run(settings.data_dir, file, 'legacy.mov', 'video/quicktime',
                           hashlib.sha256(VIDEO_BYTES).hexdigest(), None, 'legacy-fingerprint', 'ui')['job_id']
    db.complete_run(settings.data_dir, job_id, *fixture_reports(job_id))
    scope = client.get(f'/ui/v1/runs/{job_id}').json()['analysis_scope']
    assert scope['mode'] == 'unknown'
    assert scope['checked_rule_ids'] == []
    assert scope['rule_coverage_known'] is False


def test_upload_limit_idempotency_and_queue_capacity(service, monkeypatch):
    api, client, settings = service
    login(client)
    monkeypatch.setattr(api, 'settings', replace(settings, max_queue=1, max_upload_bytes=1024))
    first = submit(client, key='one-intent')
    assert first.status_code == 200
    again = submit(client, key='one-intent')
    assert again.status_code == 200 and again.json()['job_id'] == first.json()['job_id']
    conflict = submit(client, key='one-intent', payload=VIDEO_BYTES + b'different')
    assert conflict.status_code == 409
    assert submit(client, key='too-large', payload=VIDEO_BYTES * 100).status_code == 413
    assert submit(client, key='new-intent').status_code == 503
    assert client.get('/ui/v1/runs').json()['total'] == 1


def test_strict_jury_schema_and_failed_state(service):
    _, client, settings = service
    response = submit(client, ui=False)
    assert response.status_code == 200
    assert set(response.json()) == {'job_id', 'status'}
    job_id = response.json()['job_id']
    route = f'/api/v1/jobs/{job_id}'
    assert client.get(route).status_code == 401
    pending = client.get(route, headers={'X-API-Key': KEY}).json()
    assert pending == {'job_id': job_id, 'status': 'processing', 'verdict': None, 'violations': []}
    assert db.complete_run(settings.data_dir, job_id, *fixture_reports(job_id))
    completed = client.get(route, headers={'X-API-Key': KEY}).json()
    assert set(completed) == {'job_id', 'status', 'verdict', 'violations'}
    assert completed['status'] == 'done' and completed['verdict'] == 'violation'
    assert set(completed['violations'][0]) == {'type', 'timecode_sec', 'description', 'evidence_frames'}
    failed = submit(client, ui=False, key='failure-intent').json()['job_id']
    db.fail(settings.data_dir, failed, 'model unavailable')
    failure = client.get(f'/api/v1/jobs/{failed}', headers={'X-API-Key': KEY}).json()
    assert failure['status'] == 'failed' and failure['verdict'] is None and failure['violations'] == []
    assert failure['error'] == 'model unavailable'


def test_persistent_history_review_pagination_and_protected_range_video(service):
    api, client, settings = service
    login(client)
    first = submit(client, filename='Банк A.mov').json()['job_id']
    db.complete_run(settings.data_dir, first, *fixture_reports(first))
    run = client.get(f'/ui/v1/runs/{first}').json()
    second_response = client.post(f'/ui/v1/runs/{first}/reanalyze', headers={'Idempotency-Key': 'explicit-repeat'})
    assert second_response.status_code == 200
    second = second_response.json()['job_id']
    assert second != first
    second_run = client.get(f'/ui/v1/runs/{second}').json()
    assert second_run['asset']['id'] == run['asset']['id']
    assert client.get('/ui/v1/runs', params={'limit': 1}).json()['total'] == 2
    assert len(client.get('/ui/v1/runs', params={'limit': 1, 'offset': 1}).json()['items']) == 1
    assert client.get('/ui/v1/runs', params={'search': 'бАНК', 'status': 'completed'}).json()['total'] == 1
    event_id = run['result']['violations'][0]['event_id']
    reviewed = client.patch(f'/ui/v1/runs/{first}/review', json={
        'review_status': 'reviewed', 'last_playback_sec': 2.5, 'reviewed_event_ids': [event_id]})
    assert reviewed.status_code == 200
    assert client.get('/ui/v1/runs', params={'review_status': 'reviewed'}).json()['total'] == 1
    assert client.patch(f'/ui/v1/runs/{first}/review', json={'reviewed_event_ids': ['wrong-run']}).status_code == 400
    video_route = f'/ui/v1/runs/{first}/video'
    head = client.head(video_route)
    assert head.status_code == 200 and not head.content
    assert int(head.headers['content-length']) == len(VIDEO_BYTES)
    fragment = client.get(video_route, headers={'Range': 'bytes=0-7'})
    assert fragment.status_code == 206
    assert fragment.content == VIDEO_BYTES[:8]
    assert fragment.headers['content-range'] == f'bytes 0-7/{len(VIDEO_BYTES)}'
    assert client.get(video_route, headers={'Range': 'bytes=999999-'}).status_code == 416
    client.cookies.clear()
    assert client.get(video_route).status_code == 401
    assert client.get(f'/ui/v1/runs/{first}').status_code == 401
    login(client)
    assert client.get(f'/ui/v1/runs/{first}').json()['last_playback_sec'] == 2.5
    assert client.delete(f'/ui/v1/runs/{first}').status_code == 200
    assert client.get(video_route).status_code == 404
    assert client.get(f'/ui/v1/runs/{second}/video').content == VIDEO_BYTES
    assert client.delete(f'/ui/v1/runs/{second}').status_code == 200
    assert not any((settings.data_dir / 'assets').iterdir())
    assert client.get('/ui/v1/runs').json()['total'] == 0


def generate_video(path, covered):
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'mp4v'), 20, (192, 128))
    assert writer.isOpened(), 'OpenCV mp4v encoder must be available for integration test'
    random = np.random.default_rng(42)
    texture = random.integers(50, 205, (128, 192, 3), dtype=np.uint8)
    try:
        for frame in range(80):
            image = texture.copy()
            cv2.rectangle(image, (frame % 130, 30), (frame % 130 + 28, 55), (245, 150, 30), -1)
            if covered and 20 <= frame < 50:
                image[:] = 0
            writer.write(image)
    finally:
        writer.release()
    return path.read_bytes()


@pytest.mark.parametrize('covered', [True, False], ids=['real-obstruction', 'real-clear-video'])
def test_real_video_subprocess_analysis_and_decodable_evidence(service, tmp_path, covered):
    api, client, settings = service
    login(client)
    payload = generate_video(tmp_path / 'actual.mp4', covered)
    accepted = submit(client, filename='actual.mp4', payload=payload)
    assert accepted.status_code == 200
    run_id = accepted.json()['job_id']
    job = db.claim_next(settings.data_dir)
    assert job['id'] == run_id
    process_integrated_job(job, settings)
    response = client.get(f'/ui/v1/runs/{run_id}')
    assert response.status_code == 200
    run = response.json()
    assert run['status'] == 'done', run.get('error')
    assert run['archive_status'] == 'saved'
    result = run['result']
    assert result['detectors'] == ['obstruction']
    assert result['analysis_scope']['checked_rule_ids'] == ['V13']
    assert result['analysis_scope']['provenance'] == 'completed_run'
    assert run['analysis_scope'] == result['analysis_scope']
    assert not result['degraded'] and result['coverage_sec'] >= 3.9
    assert result['video']['duration_sec'] == pytest.approx(4, abs=.05)
    assert result['activity'] and result['thumbs']
    expected = 'violation' if covered else 'no_violation'
    assert result['verdict'] == expected
    assert bool(result['violations']) is covered
    if covered:
        finding = result['violations'][0]
        assert finding['rule']['id'] == 'V13' and 'п.' in finding['description']
        assert 0 <= finding['timecode'] <= 4 and 1 <= len(finding['frames']) <= 3
        for frame in finding['frames']:
            assert 0 <= frame['timestamp_sec'] <= 4
            with Image.open(io.BytesIO(base64.b64decode(frame['image_base64'], validate=True))) as image:
                assert image.format == 'JPEG'
                image.verify()
    jury = client.get(f'/api/v1/jobs/{run_id}', headers={'X-API-Key': KEY}).json()
    assert jury['verdict'] == expected and jury['status'] == 'done'
    # Opening after a new HTTP session reads saved report and media; no new run.
    client.cookies.clear()
    login(client)
    reopened = client.get(f'/ui/v1/runs/{run_id}').json()
    assert reopened['result'] == result
    assert client.get(f'/ui/v1/runs/{run_id}/video').content == payload
    assert client.get('/ui/v1/runs').json()['total'] == 1
    # Re-entering application lifespan reconstructs the archive from SQLite.
    with TestClient(api.app) as restarted_client:
        login(restarted_client)
        restored = restarted_client.get(f'/ui/v1/runs/{run_id}').json()
        assert restored['result'] == result
        assert restored['asset']['available']
        assert restarted_client.get('/ui/v1/runs').json()['total'] == 1


def test_staged_save_retry_never_calls_analyzer(service, monkeypatch):
    _, client, settings = service
    login(client)
    run_id = submit(client).json()['job_id']
    job = db.claim_next(settings.data_dir)
    api_result, result = fixture_reports(run_id)
    stage = settings.data_dir / 'staged' / f'{run_id}.json'
    stage.write_text(json.dumps({'api': api_result, 'result': result,
                                'execution_token': job['execution_token']}), encoding='utf-8')
    db.mark_save_failed(settings.data_dir, run_id)
    def unexpected(*args, **kwargs):
        raise AssertionError('Saving/opening an existing report must not invoke the analyzer')
    monkeypatch.setattr('app.worker.process_integrated_job', unexpected)
    monkeypatch.setattr('app.oko.pipeline.analyzer.analyze', unexpected)
    before = client.get(f'/ui/v1/runs/{run_id}').json()
    assert before['archive_status'] == 'save_failed' and before['status'] == 'done'
    saved = client.post(f'/ui/v1/runs/{run_id}/save')
    assert saved.status_code == 200 and saved.json()['archive_status'] == 'saved'
    assert saved.json()['result']['notes'] == ['fixture saved without model']
    assert not stage.exists()
    assert client.get('/ui/v1/runs').json()['total'] == 1


def test_legacy_failed_oko_report_preserves_error_in_jury_api(service):
    _, client, settings = service
    folder = settings.data_dir / 'jobs' / 'legacyfailure'
    folder.mkdir(parents=True)
    raw = {'job_id': 'legacyfailure', 'status': 'failed', 'verdict': None,
           'violations': [], 'error': 'Legacy decoder could not read this video'}
    (folder / 'api.json').write_text(json.dumps(raw), encoding='utf-8')
    db.init(settings.data_dir)
    response = client.get('/api/v1/jobs/legacyfailure', headers={'X-API-Key': KEY})
    assert response.status_code == 200
    assert response.json() == raw


def test_untrusted_multipart_mime_cannot_create_inline_html_response(service):
    _, client, _ = service
    login(client)
    payload = VIDEO_BYTES + b'<script>window.untrustedUploadExecuted=true</script>'
    uploaded = client.post('/ui/v1/analyze', headers={'Idempotency-Key': 'hostile-mime'},
                           files={'file': ('untrusted.mp4', payload, 'text/html')})
    assert uploaded.status_code == 200
    run_id = uploaded.json()['job_id']
    media = client.get(f'/ui/v1/runs/{run_id}/video')
    assert media.status_code == 200
    assert media.headers['content-type'].split(';', 1)[0] == 'video/mp4'
    assert media.headers['x-content-type-options'] == 'nosniff'
    assert media.content == payload
