// Bitmap glyph renderer adapted from AmirBro0/2, intro.js at 8983c777.
// The source font sheets are kept locally; no external font service is used.
const fontDirectory = new URL('./assets/intro-font/', import.meta.url);
let sharedFont;

async function readFont() {
  const response = await fetch(new URL('glyphs.json', fontDirectory));
  if (!response.ok) throw new Error('Intro font is unavailable');
  const definitions = await response.json();
  const names = ['nonlatin_european', 'accented', 'ascii'];
  const images = await Promise.all(names.map(name => new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = async () => {
      try { await image.decode(); } catch { /* A loaded image can still be drawn. */ }
      resolve(image);
    };
    image.onerror = () => reject(new Error('Intro font sheet is unavailable'));
    image.src = new URL(`${name}.png`, fontDirectory).href;
  })));
  const glyphs = new Map();
  names.forEach((name, index) => {
    const image = images[index];
    const {chars, ascent, height} = definitions[name];
    const cellWidth = image.width / 16;
    const cellHeight = image.height / chars.length;
    const sheet = document.createElement('canvas');
    sheet.width = image.width;
    sheet.height = image.height;
    const context = sheet.getContext('2d', {willReadFrequently: true});
    context.drawImage(image, 0, 0);
    const pixels = context.getImageData(0, 0, image.width, image.height).data;
    chars.forEach((row, rowIndex) => {
      Array.from(row).forEach((character, columnIndex) => {
        if (!character || character === '\u0000' || glyphs.has(character)) return;
        let rightmost = -1;
        for (let y = 0; y < cellHeight; y++) {
          for (let x = 0; x < cellWidth; x++) {
            const alpha = ((rowIndex * cellHeight + y) * image.width + columnIndex * cellWidth + x) * 4 + 3;
            if (pixels[alpha] > 0) rightmost = Math.max(rightmost, x);
          }
        }
        if (rightmost < 0) return;
        glyphs.set(character, {
          image, x: columnIndex * cellWidth, y: rowIndex * cellHeight,
          cellWidth, cellHeight, ascent, height,
          advance: Math.ceil((rightmost + 1) * height / cellHeight) + 1,
        });
      });
    });
  });
  const advance = character => character === ' ' ? 4 : (glyphs.get(character) || glyphs.get('?'))?.advance || 8;
  return {
    glyphs,
    advance,
    widthOf: text => Array.from(text).reduce((width, character) => width + advance(character), 0),
  };
}

/** Render only the intro labels, buttons and field using the reference glyphs. */
export class IntroFont {
  constructor(root = document) {
    this.root = root;
    this.font = null;
    this.texts = new WeakMap();
    sharedFont ||= readFont();
    this.ready = sharedFont.then(font => {
      this.font = font;
      this.renderStatic();
      return true;
    }).catch(() => false);
  }

  widthOf(text) {
    return this.font ? this.font.widthOf(text) : Array.from(text).length * 6;
  }

  renderStatic() {
    this.root.querySelectorAll('[data-intro-text]').forEach(element => {
      const text = this.texts.get(element) ?? element.textContent.trim();
      this.render(element, text);
    });
  }

  render(element, text) {
    if (!element) return;
    text = String(text ?? '');
    this.texts.set(element, text);
    if (!this.font || !text) {
      element.textContent = text;
      return;
    }
    const scale = Number(element.dataset.introScale || element.dataset.minecraftScale || 2);
    const maximum = element.hasAttribute('data-intro-wrap') && element.clientWidth
      ? Math.max(1, element.clientWidth / scale) : Infinity;
    const lines = [''];
    for (const word of text.split(' ')) {
      const last = lines.length - 1;
      const candidate = lines[last] ? `${lines[last]} ${word}` : word;
      if (lines[last] && this.widthOf(candidate) > maximum) lines.push(word);
      else lines[last] = candidate;
    }
    const canvas = document.createElement('canvas');
    canvas.className = 'mc-glyphs';
    canvas.setAttribute('aria-hidden', 'true');
    canvas.width = Math.max(1, Math.ceil(Math.max(...lines.map(line => this.widthOf(line))) * scale));
    canvas.height = Math.ceil((12 * lines.length + 2 * (lines.length - 1)) * scale);
    const context = canvas.getContext('2d');
    context.imageSmoothingEnabled = false;
    lines.forEach((line, lineIndex) => {
      let cursor = 0;
      for (const character of line) {
        const glyph = character === ' ' ? null : this.font.glyphs.get(character) || this.font.glyphs.get('?');
        if (glyph) {
          context.drawImage(
            glyph.image, glyph.x, glyph.y, glyph.cellWidth, glyph.cellHeight,
            cursor * scale, (lineIndex * 14 + 10 - glyph.ascent) * scale,
            glyph.cellWidth * scale, glyph.height * scale,
          );
        }
        cursor += this.font.advance(character);
      }
    });
    context.globalCompositeOperation = 'source-in';
    context.fillStyle = getComputedStyle(element).color;
    context.fillRect(0, 0, canvas.width, canvas.height);
    const accessibleText = document.createElement('span');
    accessibleText.className = 'sr-only';
    accessibleText.textContent = text;
    element.replaceChildren(accessibleText, canvas);
  }
}
