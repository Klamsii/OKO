"""Check the released frontend in installed Chrome, including a poisoned old cache.

Uses a fresh, temporary Playwright context, never the user's Chrome profile. A
loopback proxy serves only public frontend resources; session requests receive a
synthetic unauthenticated response. No key, archive, video, or detector is used.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_MODULES = {"app.js", "config.js", "worlds.js", "audio.js", "archive.js",
                    "report.js", "controls.js", "analysis-scope.js", "intro-flow.js", "intro-font.js"}


@contextmanager
def public_proxy(upstream: str):
    """Warm deliberately broken legacy assets without changing the real server."""
    modules = sorted(path.name for path in (ROOT / "web").glob("*.js"))
    styles = sorted(path.name for path in (ROOT / "web").glob("*.css"))
    legacy_hits: Counter[str] = Counter()
    unexpected: list[str] = []
    requests: list[dict] = []
    state = {"phase": "warm"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, status: int, body: bytes, headers: dict[str, str]):
            self.send_response(status)
            for name, value in headers.items():
                if name.lower() not in {"content-length", "connection", "transfer-encoding"}:
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            requests.append({"phase": state["phase"], "path": path})
            if path == "/__qa_seed":
                links = "".join(f'<link rel="stylesheet" href="/static/{name}">' for name in styles)
                body = (f'<!doctype html><meta charset="utf-8">{links}'
                        '<body>Deliberately stale cache fixture'
                        '<script type="module" src="/static/app.js"></script>').encode()
                return self.reply(200, body, {"Content-Type": "text/html; charset=utf-8",
                                              "Cache-Control": "no-store"})
            if path.startswith("/static/"):
                legacy_hits[path] += 1
                if path.endswith(".css"):
                    body = ("body{font-family:Arial!important;background:#91a7a2!important}"
                            ".wood-sign{background:none!important}.oko-wordmark{filter:grayscale(1)}"
                            "#landscape{display:none!important}").encode()
                    kind = "text/css; charset=utf-8"
                elif path == "/static/app.js":
                    imports = "".join(f"import './{name}';" for name in modules if name != "app.js")
                    body = (imports + "window.__legacyModuleLoaded=true;").encode()
                    kind = "application/javascript; charset=utf-8"
                elif path.endswith(".js"):
                    body, kind = b"export {};", "application/javascript; charset=utf-8"
                else:
                    unexpected.append(self.path)
                    return self.reply(404, b"Unknown legacy fixture", {})
                return self.reply(200, body, {"Content-Type": kind,
                                              "Cache-Control": "public, max-age=31536000, immutable"})
            if path == "/ui/v1/session":
                return self.reply(401, b'{"detail":"Isolated public frontend QA"}',
                                  {"Content-Type": "application/json", "Cache-Control": "no-store"})
            if path == "/favicon.ico":
                return self.reply(204, b"", {"Cache-Control": "no-store"})
            if path != "/" and not path.startswith("/_ui/"):
                unexpected.append(self.path)
                return self.reply(403, b"Only public frontend resources are permitted", {})
            # No user cookies, authorization, or browser headers are forwarded.
            try:
                with urlopen(Request(upstream + self.path,
                                     headers={"Accept-Encoding": "identity"}), timeout=30) as response:
                    return self.reply(response.status, response.read(), dict(response.headers.items()))
            except HTTPError as error:
                return self.reply(error.code, error.read(), {"Content-Type": "text/plain"})

        def do_POST(self):
            unexpected.append("POST " + self.path)
            self.reply(405, b"No mutations are permitted", {})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state, legacy_hits, unexpected, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8766")
    args = parser.parse_args()
    parsed = urlsplit(args.url)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.username or parsed.password or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment):
        parser.error("Use a plain local application origin, such as http://127.0.0.1:8766")
    upstream = args.url.rstrip("/")
    report = {"checks": [], "errors": [], "failed_requests": [], "bad_responses": [],
              "release_assets": []}
    output = ROOT / "qa" / "chrome-asset-release-results.json"

    def check(condition, message):
        if not condition:
            raise AssertionError(message)
        report["checks"].append(message)

    try:
        with public_proxy(upstream) as (origin, state, legacy_hits, unexpected, requests):
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="chrome", headless=True)
                report["browser_version"] = browser.version
                context = browser.new_context(viewport={"width": 1920, "height": 1030},
                                              reduced_motion="reduce")
                page = context.new_page()
                # Warm deliberately bad, long-lived copies of every legacy JS/CSS path.
                page.goto(origin + "/__qa_seed?pass=1", wait_until="networkidle")
                page.wait_for_function("window.__legacyModuleLoaded===true")
                warmed = dict(legacy_hits)
                check(bool(warmed) and "/static/app.js" in warmed and "/static/styles.css" in warmed,
                      "Broken legacy JavaScript and CSS were loaded into the Chrome HTTP cache")
                page.goto(origin + "/__qa_seed?pass=2", wait_until="networkidle")
                page.wait_for_function("window.__legacyModuleLoaded===true")
                check(dict(legacy_hits) == warmed,
                      "Repeated seed navigation used cached legacy assets without contacting the proxy")
                report["warmed_legacy_paths"] = sorted(warmed)
                state["phase"] = "release"
                page.on("pageerror", lambda error: report["errors"].append(str(error)))
                page.on("requestfailed", lambda request: report["failed_requests"].append(
                    {"url": request.url, "error": request.failure}))

                def record_response(response):
                    path = urlsplit(response.url).path
                    if path.startswith("/_ui/"):
                        report["release_assets"].append({"path": path, "status": response.status,
                            "type": response.request.resource_type,
                            "content_type": response.headers.get("content-type", "")})
                    if response.status >= 400 and path != "/ui/v1/session":
                        report["bad_responses"].append({"path": path, "status": response.status})

                page.on("response", record_response)
                response = page.goto(origin + "/", wait_until="networkidle")
                check(response is not None and response.status == 200, "Released root page returned HTTP 200")
                revision = response.headers.get("x-ui-version", "")
                check(bool(revision), "Root page declares its frontend release revision")
                prefix = f"/_ui/{revision}/"
                report["revision"] = revision
                page.wait_for_function("document.fonts.check('12px Pixel')")
                page.wait_for_function("""() => {
                    const c=document.getElementById('landscape');
                    if(c.width!==640 || c.height!==360)return false;
                    const d=c.getContext('2d').getImageData(0,0,c.width,c.height).data;
                    const colors=new Set();for(let i=0;i<d.length;i+=4*101)
                        colors.add(`${d[i]},${d[i+1]},${d[i+2]}`);
                    return colors.size>32;
                }""")
                page.wait_for_function("document.getElementById('typed-message').textContent.length>10")
                appearance = page.evaluate("""async () => {
                    const canvas=document.getElementById('landscape'),logo=document.querySelector('.oko-wordmark');
                    const entry=[...document.scripts].find(s=>s.type==='module'&&s.src)?.src;
                    const config=(await import(new URL('./config.js',entry).href)).CONFIG;
                    return {
                        font:getComputedStyle(document.body).fontFamily,
                        fontLoaded:document.fonts.check('12px Pixel'),
                        wood:getComputedStyle(document.querySelector('.wood-sign')).backgroundImage,
                        fallback:getComputedStyle(canvas).backgroundImage,
                        canvas:[canvas.width,canvas.height],
                        logo:logo.currentSrc,logoLoaded:logo.complete&&logo.naturalWidth>0,
                        logoIntrinsicSize:[logo.naturalWidth,logo.naturalHeight],
                        greeting:document.getElementById('typed-message').textContent,
                        styles:[...document.querySelectorAll('link[rel=stylesheet]')].map(l=>l.href),
                        scripts:[...document.scripts].filter(s=>s.src).map(s=>s.src),
                        configuredAssets:[...Object.values(config.audio.tracks).map(t=>t.url),config.credits.url],
                    };
                }""")
                report["appearance"] = appearance
                check(appearance["fontLoaded"] and "Pixel" in appearance["font"],
                      "Pixel font is loaded and used by the body")
                check(prefix in appearance["wood"] and "oak-hanging-sign.svg" in appearance["wood"],
                      "Wooden sign texture resolves inside the current release")
                check(prefix in appearance["fallback"] and "cherry-grove.png" in appearance["fallback"],
                      "Cherry grove CSS background resolves inside the current release")
                check(appearance["canvas"] == [640, 360], "JavaScript painted the 640×360 cherry grove background")
                check(appearance["logoLoaded"] and prefix in appearance["logo"],
                      "The released oko wordmark loaded successfully")
                with urlopen(upstream + urlsplit(appearance["logo"]).path, timeout=20) as logo_response:
                    logo = logo_response.read().decode("utf-8")
                check('viewBox="0 0 352 112"' in logo and 'id="o-outline"' in logo and 'id="k-outline"' in logo,
                      "The released sign uses the uppercase Ender-eye wordmark")
                for key in ("styles", "scripts", "configuredAssets"):
                    check(bool(appearance[key]) and all(urlsplit(url).path.startswith(prefix)
                                                       for url in appearance[key]),
                          f"All {key} resolve inside one frontend release directory")
                loaded = {item["path"].rsplit("/", 1)[-1] for item in report["release_assets"]}
                check(EXPECTED_MODULES.issubset(loaded), "The full expected ES module graph loaded")
                font_responses = [item for item in report["release_assets"] if item["path"].endswith(".ttf")]
                check(bool(font_responses) and all(item["content_type"].startswith("font/ttf")
                                                  for item in font_responses), "Released font has the correct MIME type")
                check(all(item["path"].startswith(prefix) for item in report["release_assets"]),
                      "No resource from another release was mixed into the page")
                check(dict(legacy_hits) == warmed,
                      "The released page never requested a poisoned legacy /static resource")
                page.screenshot(path=str(ROOT / "qa" / "chrome-asset-release.png"), full_page=True)
                page.reload(wait_until="networkidle")
                page.wait_for_function("document.fonts.check('12px Pixel')")
                check(page.locator(".oko-wordmark").evaluate("el=>el.complete&&el.naturalWidth>0"),
                      "Reload preserves the released logo and font")
                no_js_context = browser.new_context(java_script_enabled=False,
                    viewport={"width": 1366, "height": 900}, reduced_motion="reduce")
                no_js_page = no_js_context.new_page()
                no_js_page.goto(origin + "/", wait_until="networkidle")
                no_js = no_js_page.locator("#landscape").evaluate("el=>getComputedStyle(el).backgroundImage")
                check(prefix in no_js and "cherry-grove.png" in no_js,
                      "CSS still supplies the landscape when JavaScript is unavailable")
                check(bool(no_js_page.locator("#typed-message").inner_text()),
                      "The greeting remains readable without JavaScript")
                no_js_page.screenshot(path=str(ROOT / "qa" / "chrome-asset-release-no-js.png"), full_page=True)
                no_js_context.close()
                context.close()
                browser.close()
            report["proxy_requests"] = requests
            check(not unexpected, f"Proxy saw no private requests or mutations: {unexpected}")
            check(not report["errors"], f"No uncaught JavaScript errors: {report['errors']}")
            check(not report["failed_requests"], f"No failed asset requests: {report['failed_requests']}")
            check(not report["bad_responses"], f"No unexpected HTTP errors: {report['bad_responses']}")
            report["passed"] = True
    except Exception as error:
        report["passed"] = False
        report["failure"] = str(error)
        raise
    finally:
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "checks": len(report["checks"]),
                      "revision": report["revision"], "report": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
