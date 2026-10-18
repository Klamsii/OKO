"""Real local integration UI checks. Never reads .env or uses a production key.

Start an isolated server and provide --url / --key (or FORTE_QA_KEY).
Requires Python Playwright and an installed Microsoft Edge browser.
The late-result case delays only the browser's status response; the server and
detector still process the actual uploaded fixture. No test routes enter product code.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "qa"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8766")
    parser.add_argument("--key", default=os.environ.get("FORTE_QA_KEY"))
    args = parser.parse_args()
    if not args.key:
        parser.error("Pass an explicit isolated test key with --key or FORTE_QA_KEY")
    if urlsplit(args.url).hostname not in ("127.0.0.1", "localhost"):
        parser.error("These checks must target an isolated local server")
    covered, clear = ROOT / "qa/fixtures/covered.mov", ROOT / "qa/fixtures/clear.mov"
    assert covered.is_file() and clear.is_file(), "Generate H264 QA fixtures first"
    checks, errors, posts, session_posts, missing_audio = [], [], [], [], []
    held = {"enabled": False, "id": None, "count": 0}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1366, "height": 768}, device_scale_factor=1)
        page = context.new_page()
        page.set_default_timeout(30000)
        page.add_init_script("""window.__qaGains=[];const Context=window.AudioContext||window.webkitAudioContext;if(Context){const create=Context.prototype.createGain;Context.prototype.createGain=function(){const gain=create.call(this),disconnect=gain.disconnect;gain.disconnect=function(){gain.__qaDisconnected=true;return disconnect.apply(this,arguments);};window.__qaGains.push(gain);return gain;};}""")
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: posts.append(request.url) if request.method == "POST" and ("/analyze" in request.url or "/reanalyze" in request.url) else None)
        page.on("request", lambda request: session_posts.append(request.url) if request.method == "POST" and urlsplit(request.url).path == "/ui/v1/session" else None)
        page.on("response", lambda response: missing_audio.append(response.url) if response.status == 404 and re.search(r"(end|credits).*(mp3|m4a|ogg|wav)", response.url, re.I) else None)

        def withhold_completed_status(route):
            match = re.search(r"/ui/v1/jobs/([^/]+)$", urlsplit(route.request.url).path)
            if held["enabled"] and match and match.group(1) == held["id"]:
                response = route.fetch()
                data = response.json()
                data.update(status="processing", result=None, progress=0.25, stage="detect", stage_label="Ожидание доставки результата в тесте")
                held["count"] += 1
                route.fulfill(response=response, json=data)
            else:
                route.continue_()

        page.route("**/ui/v1/jobs/*", withhold_completed_status)

        def shot(name):
            page.locator("#portal-transition").wait_for(state="hidden")
            page.screenshot(path=str(OUT / f"integration-{name}.png"), full_page=True)

        def no_horizontal_overflow():
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), page.evaluate("({scroll:document.documentElement.scrollWidth,viewport:innerWidth})")

        def open_history():
            page.locator("#nav-history").click()
            page.locator("#archive-scene").wait_for(state="visible")
            page.wait_for_function("!document.getElementById('archive-status').textContent.includes('Загрузка')")

        def wait_done():
            page.locator("#report-tools").wait_for(state="visible", timeout=90000)
            page.wait_for_function("document.querySelector('.save-status').textContent.includes('Сохранено')", timeout=90000)

        def select_filter(control_id, value):
            page.locator(f'#{control_id}').locator('..').locator('.choice-trigger').click()
            page.locator(f'.choice-menu:visible [data-value="{value}"]').click()

        page.goto(args.url, wait_until="networkidle")
        shot("overworld-1366")
        page.locator("#api-key").fill("incorrect-isolated-test-key")
        page.locator("#start-button").click()
        page.locator("#failure-screen").wait_for(state="visible")
        assert page.locator("#failure-reason").inner_text() == "Неверный X-API ключ"
        checks.append("invalid-key-rejected")
        page.locator("#failure-back").click()
        page.locator("#api-key").fill(args.key)
        page.locator("#start-button").click()
        page.locator("#checking-screen").wait_for(state="visible")
        page.locator("#loading-screen").wait_for(state="visible")
        page.locator("#workspace-scene").wait_for(state="visible", timeout=15000)
        page.locator("#portal-transition").wait_for(state="hidden")
        assert page.evaluate("location.hash") == "#analysis"
        if page.locator("#motion-toggle").get_attribute("aria-pressed") == "false":
            page.locator("#motion-toggle").click()
        assert ".mp4" in page.locator("#video-file").get_attribute("accept")
        assert "500" in page.locator("#file-limits").text_content()
        checks.append("cookie-login-and-capabilities")

        held.update(enabled=True, id=None, count=0)
        page.locator("#video-file").set_input_files(str(covered))
        with page.expect_response(lambda response: response.request.method == "POST" and urlsplit(response.url).path == "/ui/v1/analyze") as accepted:
            page.locator("#analyze-button").click()
        run_a = accepted.value.json()["job_id"]
        held["id"] = run_a
        page.wait_for_function("id=>location.hash.includes(id)", arg=run_a)
        for _ in range(25):
            if held["count"]:
                break
            page.wait_for_timeout(200)
        assert held["count"], "The test must withhold at least one actual status response"
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator("#clear-video").click()
        page.locator("#video-file").set_input_files(str(clear))
        held["enabled"] = False
        page.wait_for_timeout(3000)
        assert page.locator("#file-name").inner_text() == clear.name
        assert page.locator("#chat-log .chat-entry").count() == 0
        checks.append("real-A-clear-B-late-A-does-not-replace-B")

        before = len(posts)
        with page.expect_response(lambda response: response.request.method == "POST" and urlsplit(response.url).path == "/ui/v1/analyze") as accepted:
            page.locator("#analyze-button").evaluate("button=>{button.click();button.click();}")
        run_b = accepted.value.json()["job_id"]
        wait_done()
        assert len(posts) == before + 1
        assert page.locator("#event-count").inner_text() == "0"
        checks.append("double-click-single-job-and-real-clear-negative")
        page.locator("#clear-video").click()
        assert page.locator("#video-file").input_value() == ""
        open_history()
        page.locator(f'[data-run-id="{run_a}"] button').filter(has_text="Открыть проверку").click()
        page.locator("#workspace-scene").wait_for(state="visible")
        wait_done()
        assert len(posts) == before + 1
        assert page.locator("#event-count").inner_text() == "1"
        assert page.locator(".timeline-slot").count() <= 240
        page.wait_for_function("document.getElementById('video').readyState >= 1 || document.getElementById('video').error", timeout=15000)
        shot("reopened-video-diagnostic")
        print(json.dumps({"playback": page.locator("#video").evaluate("video=>({ready:video.readyState,error:video.error?.code,errorMessage:video.error?.message,source:video.currentSrc})")}), flush=True)
        assert page.locator("#video").evaluate("video=>video.videoWidth > 0")
        page.locator(".event-time").first.click()
        page.locator(".evidence-thumb").first.click()
        page.locator("#evidence-dialog").wait_for(state="visible")
        page.wait_for_function("document.getElementById('evidence-image').complete && document.getElementById('evidence-image').naturalWidth>0")
        assert page.locator("#evidence-image").evaluate("image=>image.complete && image.naturalWidth>0")
        page.locator("#evidence-dialog [data-close]").click()
        page.locator("#zoom-button").click()
        page.locator("#fullscreen-button").click()
        page.wait_for_function("!!document.fullscreenElement")
        page.locator("#fullscreen-button").click()
        page.wait_for_function("!document.fullscreenElement")
        assert page.locator("#zoom-button span").inner_text() == "2×"
        page.locator("#zoom-button").click()
        page.locator("#video").evaluate("video=>{video.currentTime=4.25;video.dispatchEvent(new Event('timeupdate'));}")
        page.locator('.review-segment[data-value="reviewed"]').click()
        page.wait_for_timeout(1700)
        saved = context.request.get(f"{args.url}/ui/v1/runs/{run_a}").json()
        assert saved["review_status"] == "reviewed"
        assert abs(saved["last_playback_sec"] - 4.25) < 0.4, saved["last_playback_sec"]
        assert saved["reviewed_event_ids"]
        checks.append("real-obstruction-JPEG-timeline-player-review-position")
        for width, height in [(1366, 768), (1920, 1080), (390, 844)]:
            page.set_viewport_size({"width": width, "height": height})
            no_horizontal_overflow()
            assert page.locator(".event-description").first.evaluate("node=>node.getBoundingClientRect().width") >= 180, "Episode text collapsed into narrow columns"
            shot(f"nether-{width}")
        page.set_viewport_size({"width": 1366, "height": 768})
        old_posts = len(posts)
        page.reload(wait_until="networkidle")
        wait_done()
        assert len(posts) == old_posts
        assert page.locator('.review-segment[data-value="reviewed"]').get_attribute('aria-checked') == 'true'
        assert page.locator("#file-name").inner_text() == covered.name
        checks.append("reload-auth-run-report-and-review-without-model")

        open_history()
        page.locator("#archive-search").fill("covered")
        page.wait_for_timeout(500)
        assert page.locator(f'[data-run-id="{run_a}"]').count() == 1
        select_filter('archive-review-filter', 'reviewed')
        page.wait_for_timeout(500)
        assert page.locator(f'[data-run-id="{run_a}"]').count() == 1
        for width, height in [(1366, 768), (390, 844)]:
            page.set_viewport_size({"width": width, "height": height})
            no_horizontal_overflow()
            shot(f"end-{width}")
        page.set_viewport_size({"width": 1366, "height": 768})
        select_filter('archive-review-filter', 'all')
        page.wait_for_timeout(300)
        with page.expect_response(lambda response: response.request.method == "POST" and "/reanalyze" in response.url) as accepted:
            page.locator(f'[data-run-id="{run_a}"] button').filter(has_text="Проверить снова").click()
        run_c = accepted.value.json()["job_id"]
        assert run_c != run_a
        wait_done()
        assert page.locator('.review-segment[data-value="not_reviewed"]').get_attribute('aria-checked') == 'true'
        open_history()
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator(f'[data-run-id="{run_c}"] button').filter(has_text="Удалить из истории").click()
        page.wait_for_function("selector=>!document.querySelector(selector)", arg=f'[data-run-id="{run_c}"]')
        original = context.request.get(f"{args.url}/ui/v1/runs/{run_a}").json()
        assert original["asset"]["available"]
        assert context.request.get(f"{args.url}/ui/v1/runs/{run_a}/video", headers={"Range": "bytes=0-99"}).status == 206
        checks.append("archive-search-review-filter-reanalyze-delete-shared-video")

        sound = page.locator("#sound-toggle")
        if sound.get_attribute("aria-pressed") == "false":
            sound.click()
        if page.locator("#motion-toggle").get_attribute("aria-pressed") == "true":
            page.locator("#motion-toggle").click()
        page.locator("#nav-analysis").click()
        page.locator("#portal-transition").wait_for(state="visible")
        page.locator("#portal-sound-toggle").click()
        assert sound.get_attribute("aria-pressed") == "false"
        assert page.evaluate("Array.from(document.querySelectorAll('audio')).every(audio=>audio.volume===0&&audio.paused) && (window.__qaGains||[]).filter(node=>!node.__qaDisconnected).every(node=>node.gain.value===0)")
        page.locator("#workspace-scene").wait_for(state="visible")
        open_history()
        page.locator("#end-exit-button").click()
        page.locator("#credits-scene").wait_for(state="visible")
        page.locator(".credits-name").first.wait_for()
        shot("credits-1366")
        page.locator("#credits-pause").click()
        assert page.locator("#credits-pause").inner_text() == "Продолжить"
        page.locator("#credits-sound-toggle").click()
        page.locator("#credits-sound-toggle").click()
        assert page.evaluate("Array.from(document.querySelectorAll('audio')).every(audio=>audio.volume===0&&audio.paused) && (window.__qaGains||[]).filter(node=>!node.__qaDisconnected).every(node=>node.gain.value===0)")
        page.keyboard.press("Escape")
        page.locator("#welcome-scene").wait_for(state="visible")
        assert sound.get_attribute("aria-pressed") == "false"
        login_requests = len(session_posts)
        analysis_requests = len(posts)
        assert page.locator("#api-key").input_value() == ""
        page.locator("#start-button").click()
        page.locator("#failure-screen").wait_for(state="visible")
        assert page.locator("#failure-reason").inner_text() == "X-API ключ не указан"
        assert len(session_posts) == login_requests
        page.locator("#failure-back").click()
        page.locator("#api-key").fill(args.key)
        page.locator("#start-button").click()
        page.locator("#workspace-scene").wait_for(state="visible", timeout=15000)
        assert len(session_posts) == login_requests + 1
        assert len(posts) == analysis_requests
        checks.append("explicit-key-relogin-after-credits-without-reanalysis")
        page.emulate_media(reduced_motion="reduce")
        page.set_viewport_size({"width": 390, "height": 844})
        open_history()
        page.locator("#end-exit-button").click()
        page.locator("#credits-scroll.static-credits").wait_for()
        no_horizontal_overflow()
        shot("credits-390")
        page.locator("#credits-skip").click()
        page.locator("#welcome-scene").wait_for(state="visible")
        no_horizontal_overflow()
        shot("overworld-390")
        checks.append("portals-credits-pause-skip-mute-reduced-mobile")
        assert page.evaluate("key=>![...Object.values(localStorage),...Object.values(sessionStorage)].some(value=>value.includes(key))", args.key)
        assert not missing_audio, missing_audio
        denied = browser.new_context(viewport={"width": 390, "height": 844}, reduced_motion="reduce")
        denied.add_init_script("for(const name of ['localStorage','sessionStorage'])Object.defineProperty(window,name,{get(){throw new DOMException('Blocked by QA','SecurityError');}})")
        blocked = denied.new_page()
        blocked.on("pageerror", lambda error: errors.append(str(error)))
        blocked.goto(args.url, wait_until="networkidle")
        blocked.locator("#api-key").fill(args.key)
        blocked.locator("#start-button").click()
        blocked.locator("#workspace-scene").wait_for(state="visible")
        blocked.locator("#nav-history").click()
        blocked.locator("#archive-scene").wait_for(state="visible")
        denied.close()
        checks.append("storage-denied-login-navigation-safe")
        assert not errors, errors
        checks.append("no-secret-storage-no-browser-errors")
        result = {"ok": True, "checks": checks, "browser_errors": errors, "missing_end_credits_audio": missing_audio, "analysis_requests": len(posts), "run_ids": {"late_A": run_a, "clear_B": run_b, "intentional_repeat": run_c}}
        (OUT / "integration-browser-results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=True))
        browser.close()


if __name__ == "__main__":
    main()
