import {IntroFont} from './intro-font.js';

// Connection screens and the chunk-status animation follow AmirBro0/2,
// intro.js at 8983c777. Authentication stays with this application's cookie API.
const cancelled = () => new DOMException('Cancelled', 'AbortError');

function delay(milliseconds, signal) {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(cancelled()); return; }
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', abort);
      resolve();
    }, milliseconds);
    function abort() {
      clearTimeout(timer);
      reject(cancelled());
    }
    signal.addEventListener('abort', abort, {once: true});
  });
}

function failureText(error) {
  if (error?.status === 401) return 'Неверный X-API ключ';
  if (error?.status === 403) return 'Доступ к серверу запрещён';
  if (error?.status === 429) return 'Слишком много попыток. Попробуйте позже';
  if (error?.status) return 'Сервис временно недоступен';
  return 'Локальный сервер не отвечает';
}

export class ServerIntro {
  constructor({authenticate, enter, audio, ready = () => Promise.resolve()}) {
    this.authenticate = authenticate;
    this.enter = enter;
    this.audio = audio;
    this.worldReady = ready;
    this.composition = document.getElementById('intro-composition');
    this.wrapper = document.getElementById('intro-states');
    this.welcome = document.getElementById('welcome-scene');
    this.input = document.getElementById('api-key');
    this.keyVisual = document.getElementById('key-visual');
    this.keyRender = document.getElementById('key-render');
    this.keyCursor = document.getElementById('key-cursor');
    this.failureReason = document.getElementById('failure-reason');
    this.progress = document.getElementById('load-progress');
    this.worldBuild = document.getElementById('world-build');
    this.panels = new Map(['checking', 'loading', 'failure', 'menu'].map(name => [name, document.getElementById(`${name}-screen`)]));
    this.font = new IntroFont(this.welcome);
    this._screen = 'connect';
    this._entering = false;
    this._revision = 0;
    this._controller = null;
    this._frame = 0;
    this.input.value = '';

    document.getElementById('welcome-form').addEventListener('submit', event => {
      event.preventDefault();
      this.connect();
    });
    document.getElementById('welcome-cancel').addEventListener('click', () => {
      this.click();
      this.cancel({screen: 'menu', clear: true});
    });
    document.getElementById('menu-connect-button').addEventListener('click', () => { this.click(); this.showConnect(); });
    document.getElementById('checking-cancel').addEventListener('click', () => { this.click(); this.showConnect(); });
    document.getElementById('failure-back').addEventListener('click', () => {
      this.click();
      this.showConnect();
      this.input.select();
      this.renderKey();
    });
    for (const event of ['input', 'click', 'keyup', 'select', 'focus']) {
      this.input.addEventListener(event, () => this.renderKey());
    }
    window.addEventListener('resize', () => { this.font.renderStatic(); this.renderKey(); });
    document.addEventListener('keydown', event => {
      if (event.key !== 'Escape' || this.welcome.hidden || this._entering || document.querySelector('dialog[open]')) return;
      if (this._screen === 'connect') {
        event.preventDefault();
        this.cancel({screen: 'menu', clear: true});
      } else if (this._screen === 'checking' || this._screen === 'failure') {
        event.preventDefault();
        this.showConnect();
      }
    });
    window.addEventListener('pagehide', () => this.cancel({clear: true, focus: false}));
    this.font.ready.then(() => this.renderKey());
    this.show('connect', false);
    this.renderKey();
  }

  get screen() { return this._screen; }
  get active() { return !this.welcome.hidden && this._screen !== 'connect'; }
  get entering() { return this._entering; }

  click() { this.audio?.play('click'); }

  abort() {
    this._revision++;
    this._controller?.abort();
    this._controller = null;
    cancelAnimationFrame(this._frame);
    this._frame = 0;
  }

  cancel({screen = 'connect', clear = false, focus = true} = {}) {
    this.abort();
    if (clear) this.input.value = '';
    this.show(screen, focus);
    this.renderKey();
  }

  showConnect() { this.cancel(); }

  show(screen, focus = true) {
    this._screen = screen;
    document.getElementById('welcome-error').hidden = true;
    this.welcome.dataset.introState = screen;
    this.composition.hidden = screen !== 'connect';
    this.wrapper.hidden = screen === 'connect';
    this.panels.forEach((panel, name) => { panel.hidden = name !== screen; });
    this.welcome.setAttribute('aria-busy', String(screen === 'checking' || screen === 'loading'));
    this.font.renderStatic();
    if (!focus || this.welcome.hidden) return;
    const target = screen === 'connect' ? this.input
      : screen === 'menu' ? document.getElementById('menu-connect-button')
        : screen === 'checking' ? document.getElementById('checking-cancel')
          : screen === 'failure' ? document.getElementById('failure-back') : this.panels.get('loading');
    target?.focus({preventScroll: true});
  }

  fail(reason) {
    this.font.render(this.failureReason, reason);
    this.show('failure');
  }

  renderKey() {
    // The decorative font must mask the key just like the password input.
    const value = "*".repeat(this.input.value.length);
    this.font.render(this.keyRender, value);
    const scale = Number(this.keyRender.dataset.introScale || 2);
    const position = this.input.selectionStart ?? value.length;
    const cursorX = this.font.widthOf(value.slice(0, position)) * scale;
    const totalWidth = this.font.widthOf(value) * scale;
    const offset = Math.max(0, totalWidth - this.keyVisual.clientWidth + 25);
    this.keyRender.style.transform = `translateX(${-offset}px)`;
    this.keyCursor.style.left = `${8 + cursorX - offset}px`;
  }

  drawWorld(progress) {
    const context = this.worldBuild.getContext('2d');
    context.fillStyle = '#050505';
    context.fillRect(0, 0, 104, 104);
    // This is the reference intro's decorative chunk map, not a game-world load.
    const statusColors = ['#292929', '#37576a', '#477b99', '#68872c', '#8fa752', '#b9b9b9', '#f4f4f4'];
    for (let z = 0; z < 24; z++) {
      for (let x = 0; x < 24; x++) {
        const radius = Math.max(Math.abs(x - 11.5), Math.abs(z - 11.5)) / 11.5;
        const variation = ((x * 17 + z * 29 + x * z * 7) % 11 - 5) * .009;
        const state = progress * 1.18 - radius * .88 + variation;
        const index = state < 0 ? 0 : state < .10 ? 1 : state < .18 ? 2
          : state < .28 ? 3 : state < .40 ? 4 : state < .52 ? 5 : 6;
        context.fillStyle = statusColors[index];
        context.fillRect(4 + x * 4, 4 + z * 4, 4, 4);
      }
    }
  }

  animateWorld(duration, signal) {
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    this.progress.style.width = reducedMotion ? '100%' : '0%';
    this.progress.parentElement.setAttribute('aria-valuenow', reducedMotion ? '100' : '0');
    this.drawWorld(reducedMotion ? 1 : 0);
    if (reducedMotion) return delay(duration, signal);
    const start = performance.now();
    return new Promise((resolve, reject) => {
      const abort = () => {
        cancelAnimationFrame(this._frame);
        this._frame = 0;
        reject(cancelled());
      };
      signal.addEventListener('abort', abort, {once: true});
      const frame = now => {
        if (signal.aborted) { abort(); return; }
        const progress = Math.min(1, (now - start) / duration);
        this.progress.style.width = `${Math.round(progress * 100)}%`;
        this.progress.parentElement.setAttribute('aria-valuenow', String(Math.round(progress * 100)));
        this.drawWorld(progress);
        if (progress < 1) this._frame = requestAnimationFrame(frame);
        else {
          this._frame = 0;
          signal.removeEventListener('abort', abort);
          resolve();
        }
      };
      this._frame = requestAnimationFrame(frame);
    });
  }

  async connect() {
    if (this._screen !== 'connect' || this._entering || this.welcome.hidden) return;
    const key = this.input.value.trim();
    this.abort();
    if (!key) { this.fail('X-API ключ не указан'); return; }
    this.click();
    const revision = this._revision;
    const controller = new AbortController();
    this._controller = controller;
    const {signal} = controller;
    const current = () => !signal.aborted && revision === this._revision;
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    this.show('checking');
    try {
      const [authentication] = await Promise.allSettled([
        this.authenticate(key, signal),
        delay(reducedMotion ? 150 : 950, signal),
      ]);
      if (!current()) return;
      if (authentication.status === 'rejected') throw authentication.reason;
      this.show('loading');
      await this.animateWorld(reducedMotion ? 200 : 2600, signal);
      if (!current()) return;
      await this.worldReady();
      if (!current()) return;
      this._entering = true;
      try { await this.enter(authentication.value, key); }
      finally { this._entering = false; }
      if (!current()) return;
      this.input.value = '';
      this.renderKey();
      this.show('connect', false);
    } catch (error) {
      if (current() && error?.name !== 'AbortError') this.fail(failureText(error));
    } finally {
      if (this._controller === controller) this._controller = null;
    }
  }
}
