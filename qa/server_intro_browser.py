"""Exercise the integrated server intro in isolated, real Chrome contexts.

All /ui and /api calls are intercepted. Only synthetic keys and empty history
fixtures are used; this check never reads a user key, video, cookie or database.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_KEY = "intro-qa-synthetic-key"
CAPABILITIES = {
    "formats": ["mov", "mp4"],
    "max_upload_bytes": 104857600,
    "max_video_seconds": 180,
    "deadline_sec": 240,
    "detectors": ["camera_obstruction"],
    "analysis_scope": {
        "mode": "camera_visibility",
        "label": "Проверка перекрытия камеры",
        "violation_types": ["V13"],
    },
}


class Harness:
    def __init__(self, browser, url, width=1366, height=900, reduced=True, hold_boot=False, boot_status=401):
        self.browser = browser
        self.url = url
        self.width, self.height = width, height
        self.reduced = reduced
        self.hold_boot = hold_boot
        self.boot_status = boot_status
        self.boot_release = asyncio.Event()
        self.mode = "401"
        self.posts = []
        self.held = []
        self.errors = []
        self.failed_assets = []
        self.assets = []

    async def open(self):
        self.context = await self.browser.new_context(
            viewport={"width": self.width, "height": self.height},
            reduced_motion="reduce" if self.reduced else "no-preference",
        )
        await self.context.add_init_script("""(() => {
          const original = window.fetch;
          window.__qaIgnoreSignal = false;
          window.fetch = function(url, options) {
            if (window.__qaIgnoreSignal && options?.method === 'POST' &&
                String(url).includes('/ui/v1/session')) {
              options = {...options}; delete options.signal;
            }
            return original.call(this, url, options);
          };
        })();""")
        await self.context.route("**/ui/**", self.mock_ui)
        await self.context.route("**/api/**", lambda route: route.fulfill(
            status=599, json={"detail": "Unexpected API path in isolated intro QA"}))
        self.page = await self.context.new_page()
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.on("response", self.response)
        self.page.on("requestfailed", self.request_failed)
        await self.page.goto(self.url + "/#welcome")
        await expect(self.page.locator("#welcome-scene")).to_be_visible()
        await self.page.wait_for_selector("#welcome-form label .mc-glyphs")
        if not self.hold_boot:
            await self.page.wait_for_function("document.querySelector('#api-key') === document.activeElement")
        return self

    def response(self, response):
        path = urlsplit(response.url).path
        if path.startswith(("/_ui/", "/static/")):
            self.assets.append({"path": path, "status": response.status})
            if response.status >= 400:
                self.failed_assets.append({"path": path, "status": response.status})

    def request_failed(self, request):
        path = urlsplit(request.url).path
        if path.startswith(("/_ui/", "/static/")):
            self.failed_assets.append({"path": path, "failure": request.failure})

    async def mock_ui(self, route):
        request = route.request
        path = urlsplit(request.url).path
        if path == "/ui/v1/session" and request.method == "GET":
            if self.hold_boot:
                await self.boot_release.wait()
            await route.fulfill(status=self.boot_status, json={"detail": "No fixture session"} if self.boot_status == 401
                                else {"capabilities": {**CAPABILITIES, "formats": ["invalid-boot-fixture"]}})
        elif path == "/ui/v1/session" and request.method == "POST":
            # The known synthetic key is compared, never included in the report.
            assert request.post_data_json == {"api_key": FIXTURE_KEY}
            self.posts.append({"method": "POST", "path": path, "mode": self.mode})
            mode = self.mode
            if mode == "held":
                release = asyncio.Event()
                self.held.append(release)
                await release.wait()
                try:
                    await route.fulfill(status=200, json={"capabilities": CAPABILITIES})
                except Exception:
                    pass  # An actually aborted request may already be closed.
            elif mode == "offline":
                await route.abort("connectionrefused")
            elif mode in ("401", "500"):
                await route.fulfill(status=int(mode), json={"detail": "Synthetic failure"})
            else:
                await route.fulfill(status=200, json={"capabilities": CAPABILITIES})
        elif path == "/ui/v1/runs":
            await route.fulfill(status=200, json={"items": [], "total": 0, "limit": 3, "offset": 0})
        else:
            raise AssertionError(f"Unexpected UI request in isolated intro QA: {request.method} {path}")

    async def submit(self, mode=None):
        if mode is not None:
            self.mode = mode
        await self.page.locator("#api-key").fill(FIXTURE_KEY)
        await self.page.locator("#start-button").click()

    async def screen(self, name):
        await self.page.wait_for_function(
            "name => document.getElementById('welcome-scene').dataset.introState === name", arg=name)

    async def release(self):
        for release in self.held:
            release.set()

    async def close(self):
        self.boot_release.set()
        await self.release()
        await self.context.close()

    def clean(self):
        assert not self.errors, self.errors
        assert not self.failed_assets, self.failed_assets


async def dimensions(page):
    return await page.evaluate("""() => {
      const rect = el => { const r=el.getBoundingClientRect(); return {x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom}; };
      const input=document.getElementById('api-key'), button=document.getElementById('start-button');
      const style=getComputedStyle(input), bs=getComputedStyle(button);
      const visible=e=>e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
      const glyphs=[...document.querySelectorAll('.welcome-scene .mc-glyphs')].filter(visible);
      return {
        viewport:innerWidth, documentWidth:document.documentElement.scrollWidth,
        input:rect(input),button:rect(button),
        inputBackground:style.backgroundColor,inputColor:style.color,inputHeight:style.height,
        inputBorder:style.borderTopWidth,inputBorderColor:style.borderTopColor,
        buttonBackground:bs.backgroundColor,buttonBorder:bs.borderTopWidth,
        visualColor:getComputedStyle(document.getElementById('key-visual')).color,
        overlayPointerEvents:getComputedStyle(document.getElementById('key-visual')).pointerEvents,
        renderedLabel:!!document.querySelector('#welcome-form label .mc-glyphs'),
        clipping:glyphs.filter(e=>{const r=e.getBoundingClientRect();return r.left < -1 || r.right > innerWidth+1;}).map(e=>({label:e.parentElement.textContent,rect:rect(e)})),
      };
    }""")


async def layout_case(browser, url, width, height):
    harness = await Harness(browser, url, width, height).open()
    page = harness.page
    try:
        initial = await dimensions(page)
        assert initial["documentWidth"] <= width + 1, initial
        assert not initial["clipping"], initial["clipping"]
        assert initial["inputHeight"] == "42px"
        assert initial["inputBackground"] == "rgb(8, 8, 8)"
        assert initial["inputColor"] == "rgba(0, 0, 0, 0)"
        assert initial["inputBorder"] == "2px"
        assert initial["inputBorderColor"] == "rgb(255, 255, 255)"
        assert initial["buttonBorder"] == "2px"
        assert initial["buttonBackground"] == "rgb(167, 167, 167)"
        assert initial["visualColor"] == "rgb(255, 255, 255)"
        assert initial["overlayPointerEvents"] == "none"
        assert abs(initial["input"]["width"] - min(430, width - 36)) <= 4
        assert initial["button"]["height"] == 40
        await page.locator("#api-key").fill(FIXTURE_KEY)
        await expect(page.locator("#key-render .mc-glyphs")).to_be_attached()
        assert await page.locator("#key-render .mc-glyphs").get_attribute("aria-hidden") == "true"
        await harness.submit("401")
        await harness.screen("failure")
        failed = await dimensions(page)
        assert failed["documentWidth"] <= width + 1
        assert not failed["clipping"], failed["clipping"]
        await page.locator("#failure-back").click()
        await page.locator("#welcome-cancel").click()
        await harness.screen("menu")
        menu = await dimensions(page)
        assert menu["documentWidth"] <= width + 1
        assert not menu["clipping"], menu["clipping"]
        harness.clean()
        glyph_paths = sorted({item["path"] for item in harness.assets if "/assets/intro-font/" in item["path"]})
        assert len(glyph_paths) == 4, glyph_paths
        assert all(path.startswith("/_ui/") for path in glyph_paths), glyph_paths
        backgrounds = sorted({item["path"].rsplit("/", 1)[-1] for item in harness.assets if "/assets/worlds/" in item["path"]})
        assert "cherry-grove.png" in backgrounds and not any("panorama" in item["path"] for item in harness.assets)
        return {"viewport": [width, height], "connect": initial, "failureClipping": failed["clipping"],
                "menuClipping": menu["clipping"], "glyphResources": glyph_paths, "worldResources": backgrounds}
    finally:
        await harness.close()


async def failures_case(browser, url):
    harness = await Harness(browser, url).open()
    page = harness.page
    try:
        await page.locator("#start-button").click()
        await harness.screen("failure")
        assert await page.locator("#failure-reason").inner_text() == "X-API ключ не указан"
        assert len(harness.posts) == 0
        await page.locator("#failure-back").click()
        results = {"empty": "X-API ключ не указан"}
        for mode, reason in (("401", "Неверный X-API ключ"), ("500", "Сервис временно недоступен"), ("offline", "Локальный сервер не отвечает")):
            await harness.submit(mode)
            await harness.screen("failure")
            assert await page.locator("#failure-reason").inner_text() == reason
            results[mode] = reason
            await page.locator("#failure-back").click()
            assert await page.evaluate("document.activeElement.id") == "api-key"
            assert await page.locator("#api-key").input_value() == FIXTURE_KEY
            assert await page.locator("#api-key").evaluate("e => e.selectionEnd - e.selectionStart") == len(FIXTURE_KEY)
        await harness.submit("ok")
        await expect(page.locator("#workspace-scene")).to_be_visible()
        assert len(harness.posts) == 4
        assert await page.locator("#api-key").input_value() == ""
        harness.clean()
        return {"reasons": results, "retrySuccess": True, "postCount": len(harness.posts), "keyCleared": True}
    finally:
        await harness.close()


async def cancellation_case(browser, url, action, phase="checking"):
    # Keep the response alive after abort too, to exercise the revision guard.
    harness = await Harness(browser, url, reduced=phase != "loading").open()
    page = harness.page
    try:
        if phase == "checking":
            await page.evaluate("window.__qaIgnoreSignal = true")
            await harness.submit("held")
            await harness.screen("checking")
        else:
            await harness.submit("ok")
            await harness.screen("loading")
        if action == "cancel":
            await page.locator("#checking-cancel").click()
        elif action == "escape":
            await page.keyboard.press("Escape")
        else:
            await page.locator("#brand-link").click()
        await harness.release()
        await page.wait_for_timeout(450 if phase == "checking" else 2850)
        await expect(page.locator("#workspace-scene")).to_be_hidden()
        await expect(page.locator("#welcome-scene")).to_be_visible()
        assert await page.locator("#welcome-scene").get_attribute("data-intro-state") == "connect"
        assert await page.locator("html").get_attribute("data-world") == "overworld"
        assert await page.locator("#api-key").input_value() == ("" if action == "brand" else FIXTURE_KEY)
        harness.clean()
        return {"phase": phase, "action": action, "lateResponseDidNotEnter": True, "postCount": len(harness.posts)}
    finally:
        await harness.close()


async def delayed_boot_case(browser, url, *, boot_status=401, during_checking=False):
    harness = await Harness(browser, url, hold_boot=True, boot_status=boot_status).open()
    page = harness.page
    try:
        await harness.submit("held" if during_checking else "ok")
        if during_checking:
            await harness.screen("checking")
            harness.boot_release.set()
            await page.wait_for_timeout(250)
            assert await page.locator("#welcome-scene").get_attribute("data-intro-state") == "checking"
            await harness.release()
        else:
            await expect(page.locator("#workspace-scene")).to_be_visible()
            harness.boot_release.set()
            await page.wait_for_timeout(350)
        await expect(page.locator("#workspace-scene")).to_be_visible()
        assert await page.evaluate("location.hash") == "#analysis"
        assert await page.locator("#video-file").get_attribute("accept") == ".mov,.mp4"
        assert len(harness.posts) == 1
        harness.clean()
        return {"bootStatus": boot_status, "bootReleasedDuring": "checking" if during_checking else "workspace",
                "manualSessionPreserved": True, "postCount": len(harness.posts)}
    finally:
        await harness.close()


async def timing_navigation_case(browser, url):
    harness = await Harness(browser, url, reduced=False).open()
    page = harness.page
    try:
        await page.evaluate("""() => {
          window.__introEvents=[];
          new MutationObserver(() => {
            const state=document.getElementById('welcome-scene').dataset.introState;
            if(window.__introEvents.at(-1)?.state!==state)window.__introEvents.push({state,time:performance.now()});
          }).observe(document.getElementById('welcome-scene'),{attributes:true,attributeFilter:['data-intro-state']});
        }""")
        await harness.submit("ok")
        await harness.screen("checking")
        await harness.screen("loading")
        assert await page.locator("#world-build").evaluate("c=>c.width===104 && c.height===104")
        await expect(page.locator("#workspace-scene")).to_be_visible(timeout=10000)
        await page.wait_for_function("location.hash === '#analysis'")
        events = await page.evaluate("window.__introEvents")
        checking = next(item["time"] for item in events if item["state"] == "checking")
        loading = next(item["time"] for item in events if item["state"] == "loading")
        finished = next(item["time"] for item in events if item["state"] == "connect")
        assert loading - checking >= 900, events
        assert finished - loading >= 2500, events
        assert len(harness.posts) == 1
        await page.locator("#nav-history").click()
        await expect(page.locator("#archive-scene")).to_be_visible(timeout=12000)
        assert await page.locator("html").get_attribute("data-world") == "end"
        assert await page.evaluate("location.hash") == "#history"
        await page.locator("#back-button").click()
        await expect(page.locator("#workspace-scene")).to_be_visible(timeout=12000)
        assert await page.locator("html").get_attribute("data-world") == "nether"
        assert await page.evaluate("location.hash") == "#analysis"
        harness.clean()
        return {"events": events, "checkingMs": round(loading - checking), "generationMs": round(finished - loading),
                "postCount": len(harness.posts), "netherEndBack": True, "worldCanvas": [104, 104]}
    finally:
        await harness.close()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8766")
    args = parser.parse_args()
    if urlsplit(args.url).hostname not in ("localhost", "127.0.0.1"):
        parser.error("Only the local application may be tested")
    report = {"browser": "Google Chrome", "isolatedApiFixtures": True, "checks": [], "failures": []}
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        cases = [(f"layout-{width}", lambda w=width, h=height: layout_case(browser, args.url, w, h))
                 for width, height in ((1366, 900), (1920, 1080), (390, 844), (320, 850))]
        cases += [("validation-and-retry", lambda: failures_case(browser, args.url))]
        cases += [(f"cancel-checking-{action}", lambda a=action: cancellation_case(browser, args.url, a))
                  for action in ("cancel", "escape", "brand")]
        cases += [("cancel-loading-brand", lambda: cancellation_case(browser, args.url, "brand", "loading")),
                  ("delayed-boot-401-during-checking", lambda: delayed_boot_case(browser, args.url, during_checking=True)),
                  ("delayed-boot-401-after-login", lambda: delayed_boot_case(browser, args.url)),
                  ("delayed-boot-200-after-login", lambda: delayed_boot_case(browser, args.url, boot_status=200)),
                  ("full-timing-and-navigation", lambda: timing_navigation_case(browser, args.url))]
        for name, run in cases:
            started = time.monotonic()
            try:
                details = await run()
                report["checks"].append({"name": name, "passed": True, "details": details})
                print(f"PASS {name} ({time.monotonic() - started:.1f}s)", flush=True)
            except Exception as error:
                failure = {"name": name, "error": str(error)}
                report["checks"].append({**failure, "passed": False})
                report["failures"].append(failure)
                print(f"FAIL {name}: {error}", flush=True)
        await browser.close()
    (ROOT / "qa/server-intro-results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": len(report["checks"]) - len(report["failures"]), "total": len(report["checks"]),
                      "failures": report["failures"]}, ensure_ascii=False), flush=True)
    if report["failures"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
