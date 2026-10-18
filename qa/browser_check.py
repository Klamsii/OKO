"""Local UI checks. Pass an isolated server and explicit test key; never reads .env."""
import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--url', default='http://127.0.0.1:8766')
parser.add_argument('--key', default=os.environ.get('FORTE_QA_KEY'))
args = parser.parse_args()
if not args.key:
    parser.error('Pass an explicit isolated test key with --key or FORTE_QA_KEY')
if urlsplit(args.url).hostname not in ('127.0.0.1', 'localhost'):
    parser.error('These checks must target an isolated local server')
key = args.key
output = root/'qa'
errors = []

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe', headless=True)
    page = browser.new_page(viewport={"width":1366,"height":768}, device_scale_factor=1)
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(args.url,wait_until='networkidle')
    page.screenshot(path=str(output/'welcome-1366.png'),full_page=True)
    page.locator('#api-key').fill('incorrect-key')
    page.locator('#start-button').click()
    page.locator('#failure-screen').wait_for(state='visible')
    assert page.locator('#failure-reason').inner_text() == 'Неверный X-API ключ'
    page.locator('#failure-back').click()
    page.locator('#api-key').fill(key)
    page.locator('#start-button').click()
    page.locator('#checking-screen').wait_for(state='visible')
    page.locator('#loading-screen').wait_for(state='visible')
    page.locator('#workspace-scene').wait_for(state='visible',timeout=15000)
    page.locator('#portal-transition').wait_for(state='hidden')
    page.screenshot(path=str(output/'workspace-1366.png'),full_page=True)
    page.set_viewport_size({"width":1920,"height":1080})
    page.screenshot(path=str(output/'workspace-1920.png'),full_page=True)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert page.locator('#chat-log .chat-entry').count()==0
    assert page.locator('#analyze-button').is_disabled()
    assert page.evaluate("sessionStorage.getItem('api_key')===null && localStorage.getItem('api_key')===null")
    page.locator('#back-button').click()
    page.locator('#welcome-scene').wait_for(state='visible')
    page.locator('#portal-transition').wait_for(state='hidden')
    assert page.locator('#welcome-scene').is_visible()
    mobile=browser.new_page(viewport={"width":390,"height":844},device_scale_factor=1,reduced_motion='reduce')
    mobile.on('pageerror', lambda error: errors.append(str(error)))
    mobile.goto(args.url,wait_until='networkidle')
    mobile.screenshot(path=str(output/'welcome-mobile.png'),full_page=True)
    mobile.locator('#api-key').fill(key);mobile.locator('#start-button').click()
    mobile.locator('#workspace-scene').wait_for(state='visible',timeout=15000)
    mobile.screenshot(path=str(output/'workspace-mobile.png'),full_page=True)
    print(mobile.evaluate("""Array.from(document.querySelectorAll('body *')).map(e=>({tag:e.tagName,cls:e.className?.baseVal??e.className,right:e.getBoundingClientRect().right,width:e.getBoundingClientRect().width})).filter(x=>x.right>innerWidth+1&&x.width>0).slice(0,20)"""))
    assert mobile.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert errors==[],errors
    print(json.dumps({'initial_checks':'passed','browser_errors':errors},ensure_ascii=True))
    browser.close()
