/* Lumen launch film: engine.
 * Every visual is a pure function of time t (seconds), so the same page plays live against the
 * soundtrack and renders frame-exact for video. No CSS transitions, no timers, no state that
 * accumulates between frames. */
(function () {
  'use strict';
  const TL = window.TIMELINE;
  const W = 1920, H = 1080;

  // ---------- math ----------
  const clamp = (x, a = 0, b = 1) => (x < a ? a : x > b ? b : x);
  const lerp = (a, b, k) => a + (b - a) * k;
  const E = {
    lin: (k) => k,
    inQ: (k) => k * k,
    outQ: (k) => 1 - (1 - k) * (1 - k),
    inC: (k) => k * k * k,
    outC: (k) => 1 - Math.pow(1 - k, 3),
    ioC: (k) => (k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2),
    outQ5: (k) => 1 - Math.pow(1 - k, 5),
    outE: (k) => (k >= 1 ? 1 : 1 - Math.pow(2, -10 * k)),
    inE: (k) => (k <= 0 ? 0 : Math.pow(2, 10 * k - 10)),
    ioE: (k) => (k <= 0 ? 0 : k >= 1 ? 1 : k < 0.5 ? Math.pow(2, 20 * k - 10) / 2 : (2 - Math.pow(2, -20 * k + 10)) / 2),
    outB: (k) => { const c1 = 1.4, c3 = c1 + 1; return 1 + c3 * Math.pow(k - 1, 3) + c1 * Math.pow(k - 1, 2); },
    ioS: (k) => -(Math.cos(Math.PI * k) - 1) / 2,
  };
  /** eased progress of t through [t0, t0 + d] */
  const P = (t, t0, d, e = E.outC) => e(clamp((t - t0) / (d || 1e-6)));
  /** 0..1 envelope: in over [a, a+fi], out over [b-fo, b] */
  const env = (t, a, b, fi = 0.5, fo = 0.5, ei = E.outC, eo = E.inQ) => {
    const i = P(t, a, fi, ei);
    const o = b === Infinity ? 0 : P(t, b - fo, fo, eo);
    return i * (1 - o);
  };
  const seeded = (seed) => () => {
    seed |= 0; seed = (seed + 0x6d2b79f5) | 0;
    let r = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    r = (r + Math.imul(r ^ (r >>> 7), 61 | r)) ^ r;
    return ((r ^ (r >>> 14)) >>> 0) / 4294967296;
  };
  const hash01 = (n) => { const x = Math.sin(n * 127.1 + 311.7) * 43758.5453; return x - Math.floor(x); };

  // ---------- timeline anchors ----------
  const LN = (id) => {
    const l = TL.lines[id];
    if (!l) throw new Error('unknown line ' + id);
    return l;
  };
  const norm = (s) => s.toLowerCase().replace(/[^a-z0-9]/g, '');
  /** start time of the nth word in a line that begins with `prefix` */
  const WD = (id, prefix, nth = 0) => {
    const l = LN(id); const p = norm(prefix); let k = 0;
    for (const w of l.words) {
      if (norm(w.w).startsWith(p)) { if (k === nth) return w.t; k++; }
    }
    console.warn('word not found', id, prefix);
    return l.start;
  };
  const WE = (id, prefix, nth = 0) => {
    const l = LN(id); const p = norm(prefix); let k = 0;
    for (const w of l.words) {
      if (norm(w.w).startsWith(p)) { if (k === nth) return w.t + w.d; k++; }
    }
    return l.end;
  };
  const SC = (id) => TL.scenes.find((s) => s.id === id);

  // ---------- DOM ----------
  const SVGNS = 'http://www.w3.org/2000/svg';
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    if (attrs) for (const k in attrs) {
      const v = attrs[k];
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
      else if (k === 'html') el.innerHTML = v;
      else el.setAttribute(k, v);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return el;
  }
  function s(tag, attrs, ...kids) {
    const el = document.createElementNS(SVGNS, tag);
    if (attrs) for (const k in attrs) { if (attrs[k] != null) el.setAttribute(k, attrs[k]); }
    for (const kid of kids.flat()) if (kid != null) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return el;
  }
  const $ = (root, sel) => root.querySelector(sel);

  /** write transform/opacity/filter only when changed. o: {o, x, y, s, sx, sy, r, z, blur, bright} */
  function set(el, o) {
    if (!el) return;
    const c = el.__c || (el.__c = {});
    if (o.o !== undefined) {
      const v = Math.round(clamp(o.o) * 1000) / 1000;
      if (c.o !== v) { el.style.opacity = v; c.o = v; }
      const vis = v <= 0.001 ? 'hidden' : 'visible';
      if (c.vis !== vis) { el.style.visibility = vis; c.vis = vis; }
    }
    if (o.x !== undefined || o.y !== undefined || o.s !== undefined || o.r !== undefined || o.sx !== undefined || o.z !== undefined || o.rx !== undefined || o.ry !== undefined) {
      const x = o.x || 0, y = o.y || 0, sc = o.s == null ? 1 : o.s;
      let tr = `translate3d(${x.toFixed(2)}px,${y.toFixed(2)}px,${(o.z || 0).toFixed(1)}px)`;
      if (o.rx) tr += ` rotateX(${o.rx.toFixed(2)}deg)`;
      if (o.ry) tr += ` rotateY(${o.ry.toFixed(2)}deg)`;
      if (o.r) tr += ` rotate(${o.r.toFixed(3)}deg)`;
      if (o.sx != null || o.sy != null) tr += ` scale(${(o.sx == null ? sc : o.sx).toFixed(4)},${(o.sy == null ? sc : o.sy).toFixed(4)})`;
      else if (sc !== 1) tr += ` scale(${sc.toFixed(4)})`;
      if (c.tr !== tr) { el.style.transform = tr; c.tr = tr; }
    }
    if (o.blur !== undefined || o.bright !== undefined || o.sat !== undefined) {
      let f = '';
      if (o.blur > 0.05) f += `blur(${o.blur.toFixed(2)}px) `;
      if (o.bright != null && Math.abs(o.bright - 1) > 0.005) f += `brightness(${o.bright.toFixed(3)}) `;
      if (o.sat != null && Math.abs(o.sat - 1) > 0.005) f += `saturate(${o.sat.toFixed(3)}) `;
      f = f.trim() || 'none';
      if (c.f !== f) { el.style.filter = f; c.f = f; }
    }
  }
  function css(el, prop, val) {
    if (!el) return;
    const c = el.__c || (el.__c = {});
    const key = 'p:' + prop;
    if (c[key] !== val) { el.style.setProperty(prop, val); c[key] = val; }
  }
  function text(el, str) {
    if (!el) return;
    if (el.__t !== str) { el.textContent = str; el.__t = str; }
  }
  function cls(el, name, on) {
    if (!el) return;
    const c = el.__c || (el.__c = {});
    const key = 'c:' + name;
    if (c[key] !== on) { el.classList.toggle(name, !!on); c[key] = on; }
  }

  // ---------- text effects ----------
  /** split an element's text into word spans (keeps inline <em>/<b> as single units) */
  function splitWords(el) {
    const out = [];
    const nodes = Array.from(el.childNodes);
    el.textContent = '';
    for (const n of nodes) {
      if (n.nodeType === 3) {
        const parts = n.textContent.split(/(\s+)/);
        for (const p of parts) {
          if (!p) continue;
          if (/^\s+$/.test(p)) { el.append(document.createTextNode(' ')); continue; }
          const sp = h('span', { class: 'w' }, p);
          el.append(sp); out.push(sp);
        }
      } else {
        const wrap = h('span', { class: 'w' });
        wrap.append(n); el.append(wrap); out.push(wrap);
      }
    }
    return out;
  }
  /** staggered word reveal; returns nothing */
  function wordsIn(ws, t, t0, o = {}) {
    const st = o.stagger ?? 0.055, d = o.dur ?? 0.7, dy = o.dy ?? 26, bl = o.blur ?? 10;
    const out = o.out ?? Infinity, fo = o.fo ?? 0.45;
    ws.forEach((w, i) => {
      const k = P(t, t0 + i * st, d, E.outQ5);
      const ko = out === Infinity ? 0 : P(t, out - fo + i * 0.012, fo, E.inQ);
      set(w, { o: k * (1 - ko), y: (1 - k) * dy - ko * 10, blur: (1 - k) * bl + ko * 6 });
    });
  }
  /** typewriter: shows a prefix of `str` in el, optional caret */
  function typeOn(el, str, t, t0, cps = 30, caret = true) {
    const n = clamp(Math.floor((t - t0) * cps), 0, str.length);
    const shown = t < t0 ? '' : str.slice(0, n);
    const blink = caret && (n < str.length || Math.floor((t - t0) * 1.6) % 2 === 0) && t >= t0;
    const key = shown + (blink ? '|' : '');
    if (el.__t !== key) {
      el.__t = key;
      el.textContent = shown;
      if (blink) el.append(h('span', { class: 'caret' }));
    }
    return n >= str.length;
  }

  // ---------- SVG helpers ----------
  function drawPath(path, k) {
    if (!path.__len) {
      path.__len = path.getTotalLength(); path.style.strokeDasharray = path.__len;
      path.__marker = path.getAttribute('marker-end');
    }
    k = clamp(k);
    path.__k = k;
    if (!path.__flow) css(path, 'stroke-dashoffset', ((1 - k) * path.__len).toFixed(1));
    if (path.__marker) {
      const want = k >= 0.97 ? path.__marker : null;
      if (path.__m !== want) { path.__m = want; if (want) path.setAttribute('marker-end', want); else path.removeAttribute('marker-end'); }
    }
    const vis = k > 0.002 ? 'visible' : 'hidden';
    if (path.__vis !== vis) { path.__vis = vis; path.style.visibility = vis; }
  }
  /** position along an SVG path at fraction k */
  function along(path, k) {
    if (!path.__len) path.__len = path.getTotalLength();
    const p = path.getPointAtLength(clamp(k) * path.__len);
    return { x: p.x, y: p.y };
  }

  // ---------- keyframe tracks ----------
  /** keys: [[t, x, y], ...] → {x, y} eased between keys */
  function track(t, keys, e = E.ioC) {
    if (t <= keys[0][0]) return { x: keys[0][1], y: keys[0][2] };
    for (let i = 0; i < keys.length - 1; i++) {
      const a = keys[i], b = keys[i + 1];
      if (t <= b[0]) {
        const k = e(clamp((t - a[0]) / (b[0] - a[0])));
        return { x: lerp(a[1], b[1], k), y: lerp(a[2], b[2], k) };
      }
    }
    const z = keys[keys.length - 1];
    return { x: z[1], y: z[2] };
  }

  // ---------- shared components ----------
  const ARROW = 'M5 2.5 L5 18.5 L9.2 14.9 L11.8 20.8 L14.6 19.5 L12 13.8 L17.5 13.4 Z';
  /** Lumen's in-page pointer: dark arrow, light stroke, blue glow; click ripple. */
  function Pointer(parent, scale = 2.1) {
    const root = h('div', { class: 'ptr' });
    const ripple = h('div', { class: 'ptr-ripple' });
    const arrow = h('div', { class: 'ptr-arrow' });
    arrow.append(s('svg', { viewBox: '0 0 24 24', width: 24 * scale, height: 24 * scale },
      s('path', { d: ARROW, fill: '#0b1a2e', stroke: '#eaf4ff', 'stroke-width': 1.4, 'stroke-linejoin': 'round' })));
    const chip = h('div', { class: 'ptr-chip' }, h('i'), h('span'));
    root.append(ripple, arrow, chip);
    parent.append(root);
    return {
      el: root,
      /** x,y: tip position. o: opacity. press: 0..1. clickAt: time of the last click (ripple). */
      update(t, x, y, o = 1, clickAt = -99, chipText = '') {
        set(root, { o, x, y });
        const rk = clamp((t - clickAt) / 0.56);
        const rOn = t >= clickAt && rk < 1;
        set(ripple, { o: rOn ? (1 - rk) * 0.95 : 0, s: 0.2 + 1.05 * E.outC(rk) });
        const press = t >= clickAt && t < clickAt + 0.16 ? 0.86 : 1;
        set(arrow, { s: press });
        const ct = chip.lastChild;
        if (chipText) text(ct, chipText);
        set(chip, { o: chipText ? 1 : 0 });
      },
    };
  }

  /** The desktop overlay pill: dot, "Lumen", live status line. */
  function Pill(parent) {
    const msgA = h('span', { class: 'pill-msg' });
    const msgB = h('span', { class: 'pill-msg' });
    const root = h('div', { class: 'pill' }, h('i', { class: 'pill-dot' }), h('b', null, 'Lumen'), h('span', { class: 'pill-msgs' }, msgA, msgB));
    parent.append(root);
    return {
      el: root,
      /** msgs: [[t, text], ...] — crossfades to each message at its time */
      update(t, o, msgs) {
        set(root, { o });
        let cur = 0;
        for (let i = 0; i < msgs.length; i++) if (t >= msgs[i][0]) cur = i;
        const k = P(t, msgs[cur][0], 0.35, E.outC);
        const prev = cur > 0 ? msgs[cur - 1][1] : '';
        text(msgA, msgs[cur][1]);
        text(msgB, prev);
        set(msgA, { o: cur === 0 && t < msgs[0][0] ? 0 : k, y: (1 - k) * 8 });
        set(msgB, { o: prev ? 1 - k : 0, y: -k * 8 });
        const pulse = 0.55 + 0.45 * Math.sin(t * 4.4);
        css(root, '--pulse', pulse.toFixed(3));
      },
    };
  }

  /** syntax-colored JSON, revealed character by character */
  function JsonBlock(parent, lines, cls_ = 'json') {
    // lines: array of [indent, key|null, value|null, punct]
    const root = h('div', { class: cls_ });
    const rows = lines.map((ln) => {
      const [ind, key, val, tail] = ln;
      const row = h('div', { class: 'jrow' });
      row.style.paddingLeft = (ind * 1.15) + 'em';
      const parts = [];
      if (key != null) parts.push(['jk', JSON.stringify(key)], ['jp', ': ']);
      if (val != null) {
        const kind = typeof val === 'number' ? 'jn' : (val === true || val === false) ? 'jb' : (typeof val === 'string' && /^[\[\]{}]/.test(val) && val.length <= 2) ? 'jp' : 'js';
        parts.push([kind, (kind === 'js') ? JSON.stringify(val) : String(val)]);
      }
      if (tail) parts.push(['jp', tail]);
      const spans = parts.map(([c, txt]) => { const sp = h('span', { class: c }); row.append(sp); return [sp, txt]; });
      root.append(row);
      return { row, spans, len: parts.reduce((a, p) => a + p[1].length, 0) };
    });
    parent.append(root);
    const total = rows.reduce((a, r) => a + r.len, 0);
    return {
      el: root, rows,
      /** reveal k in 0..1 of characters */
      reveal(k) {
        let n = Math.floor(total * clamp(k));
        for (const r of rows) {
          let rowShown = false;
          for (const [sp, txt] of r.spans) {
            const m = clamp(n, 0, txt.length);
            text(sp, txt.slice(0, m));
            if (m > 0) rowShown = true;
            n -= txt.length;
            if (n < 0) n = 0;
          }
          set(r.row, { o: rowShown ? 1 : 0 });
        }
      },
    };
  }

  // ---------- icons (line icons, 24 grid) ----------
  const ICONS = {
    shield: 'M12 3l7 3v5c0 4.5-3 8.3-7 10-4-1.7-7-5.5-7-10V6l7-3z|M9 12l2 2 4-4',
    zap: 'M13 2L4 14h7l-1 8 9-12h-7l1-8z',
    sparkles: 'M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3z|M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8L19 15z',
    lock: 'M6 11h12v10H6z|M8.5 11V8a3.5 3.5 0 0 1 7 0v3',
    eye: 'M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z|M12 9.2a2.8 2.8 0 1 0 0 5.6 2.8 2.8 0 0 0 0-5.6z',
    check: 'M5 12.5l4.5 4.5L19 7.5',
    x: 'M6 6l12 12|M18 6L6 18',
    browser: 'M3 5h18v14H3z|M3 9h18|M6 7h.01|M8.5 7h.01',
    monitor: 'M3 4h18v12H3z|M8 20h8|M12 16v4',
    brain: 'M9 4a3 3 0 0 0-3 3 3 3 0 0 0-2 5 3 3 0 0 0 2 5 3 3 0 0 0 6 1V4.5A1.5 1.5 0 0 0 9 4z|M15 4a3 3 0 0 1 3 3 3 3 0 0 1 2 5 3 3 0 0 1-2 5 3 3 0 0 1-6 1',
    cpu: 'M7 7h10v10H7z|M10 3v4M14 3v4M10 17v4M14 17v4M3 10h4M3 14h4M17 10h4M17 14h4',
    alert: 'M12 3l10 18H2L12 3z|M12 10v5|M12 18h.01',
    pause: 'M8 5v14|M16 5v14',
    receipt: 'M6 3h12v18l-3-2-3 2-3-2-3 2V3z|M9 8h6|M9 12h6',
    file: 'M6 3h8l4 4v14H6z|M14 3v4h4',
    terminal: 'M4 5h16v14H4z|M7 10l3 2-3 2|M12 15h5',
    hand: 'M8 13V5.5a1.5 1.5 0 0 1 3 0V12|M11 11.5V4.5a1.5 1.5 0 0 1 3 0V12|M14 11.5V6a1.5 1.5 0 0 1 3 0v7c0 4-2.5 7-6.5 7-3 0-4.5-1.5-6-4l-2-3.5a1.5 1.5 0 0 1 2.6-1.5L8 13',
  };
  function icon(name, size = 20, color = 'currentColor', sw = 1.8) {
    const svg = s('svg', { viewBox: '0 0 24 24', width: size, height: size, fill: 'none', stroke: color, 'stroke-width': sw, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', style: 'display:block;flex:none' });
    for (const d of ICONS[name].split('|')) svg.append(s('path', { d }));
    return svg;
  }
  /** layout position of el relative to an offset ancestor (ignores transforms) */
  function pos(el, anc) {
    let x = 0, y = 0, e = el;
    while (e && e !== anc) { x += e.offsetLeft; y += e.offsetTop; e = e.offsetParent; }
    return { x, y, w: el.offsetWidth, h: el.offsetHeight, cx: x + el.offsetWidth / 2, cy: y + el.offsetHeight / 2 };
  }

  // ---------- film ----------
  const FILM = {
    TL, W, H, E, P, env, clamp, lerp, seeded, hash01, LN, WD, WE, SC, h, s, $, set, css, text, cls,
    splitWords, wordsIn, typeOn, drawPath, along, track, Pointer, Pill, JsonBlock, icon, pos,
    scenes: [], t: 0,
    scene(id, def) {
      const sc = SC(id);
      if (!sc) throw new Error('no timeline scene ' + id);
      FILM.scenes.push(Object.assign({ id, start: sc.start, end: sc.end, pre: 0.7, post: 0.9, def }, sc));
    },
  };
  window.FILM = FILM;

  // ---------- global layers ----------
  let stage, grain, chapterEl, chapterTxt, chapterBar, capEl, bgGlow;
  function buildGlobals() {
    stage = document.getElementById('stage');
    const bg = h('div', { class: 'bg' });
    bgGlow = h('div', { class: 'bg-glow' });
    const grid = h('div', { class: 'bg-grid' });
    bg.append(bgGlow, grid);
    stage.prepend(bg);
    FILM.bgGrid = grid;
    chapterTxt = h('span');
    chapterBar = h('i');
    chapterEl = h('div', { class: 'chapter' }, h('b'), chapterTxt, chapterBar);
    grain = h('div', { class: 'grain' });
    const vig = h('div', { class: 'vignette' });
    capEl = h('div', { class: 'captions' });
    stage.append(chapterEl, vig, grain, capEl);
    // film grain: one noise tile, offset per 1/24 s
    const c = document.createElement('canvas'); c.width = c.height = 256;
    const g = c.getContext('2d'); const img = g.createImageData(256, 256); const rnd = seeded(7);
    for (let i = 0; i < img.data.length; i += 4) { const v = Math.floor(rnd() * 255); img.data[i] = img.data[i + 1] = img.data[i + 2] = v; img.data[i + 3] = 255; }
    g.putImageData(img, 0, 0);
    grain.style.backgroundImage = `url(${c.toDataURL()})`;
  }
  function updateGlobals(t) {
    const f = Math.floor(t * 24);
    css(grain, 'background-position', `${Math.floor(hash01(f) * 256)}px ${Math.floor(hash01(f + 91) * 256)}px`);
    // chapter label
    let cur = null;
    for (const sc of TL.scenes) if (sc.chapter && t >= sc.start && t < sc.end) cur = sc;
    if (cur) {
      text(chapterTxt, cur.chapter);
      const k = env(t, cur.start + 0.25, cur.end, 0.8, 0.6);
      set(chapterEl, { o: k * 0.92, x: (1 - P(t, cur.start + 0.25, 0.9, E.outE)) * -18 });
      css(chapterBar, 'transform', `scaleX(${clamp((t - cur.start) / (cur.end - cur.start)).toFixed(4)})`);
    } else set(chapterEl, { o: 0 });
    // background drift
    set(FILM.bgGrid, { x: -((t * 6) % 40), y: -((t * 3.5) % 40) });
    // captions (player only)
    if (FILM.captions) {
      let line = null;
      for (const id in TL.lines) { const l = TL.lines[id]; if (t >= l.start - 0.1 && t <= l.end + 0.35) { line = l; break; } }
      text(capEl, line ? line.text : '');
      set(capEl, { o: line ? 1 : 0 });
    } else set(capEl, { o: 0 });
  }

  function seek(t) {
    FILM.t = t;
    for (const sc of FILM.scenes) {
      const vis = t >= sc.start - sc.pre && t <= sc.end + sc.post;
      if (vis !== sc._vis) { sc.root.style.display = vis ? 'block' : 'none'; sc._vis = vis; }
      if (vis) {
        sc.def.update(t, sc);
        const push = sc.def.push === undefined ? 0.022 : sc.def.push;
        if (push) {
          const k = clamp((t - sc.start) / Math.max(1, sc.end - sc.start));
          set(sc.root, { s: 1 + push * E.ioS(k) });
        }
      }
    }
    updateGlobals(t);
  }
  FILM.seek = seek;

  FILM.boot = function () {
    buildGlobals();
    const host = document.getElementById('scenes');
    for (const sc of FILM.scenes) {
      sc.root = h('div', { class: 'scene', 'data-scene': sc.id });
      sc.root.style.display = 'none';
      host.append(sc.root);
      sc.def.build(sc.root, sc);
    }
    seek(0);
  };
})();
