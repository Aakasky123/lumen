/* Lumen launch film: shared UI builders, then scenes 1-2 (cold open, meet Lumen). */
(function () {
  'use strict';
  const F = window.FILM;
  const { E, P, env, clamp, lerp, h, s, set, css, text, cls, WD, WE, LN } = F;

  // =====================================================================================
  // Shared builders
  // =====================================================================================
  const ui = (F.ui = {});

  /** The Lumen mark: four slabs on a dark tile (scripts/generate-icons.py geometry). */
  ui.logo = function (size) {
    const svg = s('svg', { viewBox: '0 0 1024 1024', width: size, height: size, class: 'logo' });
    const defs = s('defs');
    const clipId = 'lg' + Math.floor(Math.random() * 1e9);
    const clip = s('clipPath', { id: clipId }, s('rect', { x: 28, y: 28, width: 968, height: 968, rx: 204 }));
    const grad = s('linearGradient', { id: clipId + 'g', x1: 0, y1: 0, x2: 1, y2: 0 },
      s('stop', { offset: 0, 'stop-color': '#ffffff', 'stop-opacity': 0 }),
      s('stop', { offset: 0.5, 'stop-color': '#ffffff', 'stop-opacity': 0.55 }),
      s('stop', { offset: 1, 'stop-color': '#ffffff', 'stop-opacity': 0 }));
    defs.append(clip, grad);
    const ring = s('rect', { width: 1024, height: 1024, rx: 228, fill: '#1c3a31' });
    const tile = s('rect', { x: 28, y: 28, width: 968, height: 968, rx: 204, fill: '#0d1f1a' });
    const g = s('g', { 'clip-path': `url(#${clipId})` });
    const slabs = [
      [212, 200, '#5ee6bd'], [372, 236, '#3fbf99'], [532, 236, '#3fbf99'], [692, 200, '#5ee6bd'],
    ].map(([x, y, c]) => s('rect', { x, y, width: 120, height: 588, rx: 26, fill: c }));
    const sweep = s('rect', { x: -400, y: -200, width: 300, height: 1500, fill: `url(#${clipId}g)`, transform: 'rotate(18 512 512)' });
    g.append(...slabs, sweep);
    svg.append(defs, ring, tile, g);
    return { svg, ring, tile, slabs, sweep };
  };

  /** A Chrome-like window (generic: no vendor marks). */
  ui.chrome = function ({ x, y, w, hh, tab = 'New tab', url = '', avatar = 'JA' }) {
    const page = h('div', { class: 'page' });
    const urlEl = h('div', { class: 'win-url' });
    urlEl.innerHTML = url;
    const av = h('div', { class: 'avatar' }, avatar);
    const bar = h('div', { class: 'win-bar' }, h('div', { class: 'win-dots' }, h('i'), h('i'), h('i')), h('div', { class: 'win-tab' }, tab), urlEl, av);
    const el = h('div', { class: 'win', style: { left: x + 'px', top: y + 'px', width: w + 'px', height: hh + 'px' } }, bar, page);
    return { el, page, bar, urlEl, av };
  };

  /** A native Windows-style window (light chrome). */
  ui.nwin = function ({ x, y, w, hh, title, menu }) {
    const tb = h('div', { class: 'nwin-title' }, h('span', null, title), h('span', { class: 'ctl' }, h('i', null, '–'), h('i', null, '▢'), h('i', null, '✕')));
    const body = h('div', { style: { position: 'absolute', left: 0, right: 0, top: menu ? '73px' : '40px', bottom: 0 } });
    const el = h('div', { class: 'nwin', style: { left: x + 'px', top: y + 'px', width: w + 'px', height: hh + 'px' } }, tb);
    if (menu) el.append(h('div', { class: 'nwin-menu' }, ...menu.map((m) => h('span', null, m))));
    el.append(body);
    return { el, body };
  };

  /** A Windows desktop: wallpaper, taskbar. */
  ui.desktop = function ({ x, y, w, hh }) {
    const el = h('div', { class: 'abs', style: { left: x + 'px', top: y + 'px', width: w + 'px', height: hh + 'px', borderRadius: '14px', overflow: 'hidden',
      background: 'radial-gradient(1200px 700px at 30% 20%, #1b2a44, #0c1220 60%, #070a12)', boxShadow: '0 50px 140px rgba(0,0,0,.7), 0 0 0 1px rgba(255,255,255,.07)' } });
    const layer = h('div', { class: 'abs', style: { inset: '0' } });
    const icons = ['#3b82f6', '#f59e0b', '#22c55e', '#e5e7eb', '#a78bfa'];
    const task = h('div', { class: 'abs', style: { left: 0, right: 0, bottom: 0, height: '56px', background: 'rgba(16,20,30,.86)', borderTop: '1px solid rgba(255,255,255,.08)', display: 'flex', justifyContent: 'center', alignItems: 'center', gap: '14px' } },
      ...icons.map((c) => h('i', { style: { width: '30px', height: '30px', borderRadius: '8px', background: c, opacity: 0.85, display: 'block' } })));
    el.append(layer, task);
    return { el, layer, task };
  };

  /** The Acme job application page used across the loop / grounding / floors scenes. */
  ui.jobPage = function (page) {
    page.style.background = '#ffffff';
    const wrap = h('div', { class: 'abs', style: { left: '0', right: '0', top: '0' } });
    const head = h('div', { style: { padding: '30px 48px 22px', borderBottom: '1px solid #eaeef2' } },
      h('div', { style: { font: '600 15px/1 var(--body)', letterSpacing: '.08em', textTransform: 'uppercase', color: '#57606a' } }, 'Acme Robotics · Careers'),
      h('div', { style: { font: '700 34px/1.15 var(--display)', letterSpacing: '-.02em', color: '#1f2328', marginTop: '10px' } }, 'Software Engineer, Robotics Platform'),
      h('div', { style: { font: '400 18px/1.4 var(--body)', color: '#57606a', marginTop: '6px' } }, 'Remote · Full time'));
    const body = h('div', { style: { position: 'relative', margin: '26px 48px', height: '760px' } });
    wrap.append(head, body);
    page.append(wrap);
    const fields = {};
    const field = (key, label, req, value, x, y, w, extra = {}) => {
      const box = h('div', { class: 'abs', style: { left: x + 'px', top: y + 'px', width: w + 'px' } });
      const lab = h('div', { class: 'f-label' }, label, req ? h('s', null, ' *') : null);
      const inp = h('div', { class: 'f-input' + (extra.select ? ' sel' : '') });
      const val = h('span');
      inp.append(val);
      if (extra.select) inp.append(h('span', { style: { position: 'absolute', right: '14px', top: '0', color: '#57606a' } }, '▾'));
      box.append(lab, inp);
      body.append(box);
      fields[key] = { box, lab, inp, val, x, y, w, value, ph: extra.ph || '' };
      text(val, value || '');
      if (!value && extra.ph) { val.className = 'ph'; text(val, extra.ph); }
      return fields[key];
    };
    field('first', 'First name', true, 'Jordan', 0, 0, 360);
    field('last', 'Last name', true, 'Avery', 390, 0, 360);
    field('email', 'Email', true, 'jordan.avery@example.com', 0, 96, 750);
    field('cc', 'Country code', false, '+1', 0, 192, 170, { select: true });
    field('phone', 'Phone', true, '', 200, 192, 550);
    field('resume', 'Resume/CV', true, '', 0, 288, 750, { ph: 'Attach  ·  PDF, DOCX' });
    field('auth', 'Are you legally authorized to work in the US?', true, '', 0, 384, 750, { select: true, ph: 'Select…' });
    field('python', 'How many years have you used Python in production?', true, '', 0, 480, 750);
    field('pronouns', 'Pronouns', false, '', 0, 576, 750, { ph: 'Optional' });
    const submit = h('div', { class: 'f-btn abs', style: { left: '0px', top: '676px' } }, 'Submit application');
    body.append(submit);
    return { wrap, body, fields, submit, headH: 158 };
  };

  // =====================================================================================
  // Scene 1: cold open
  // =====================================================================================
  const FRAG = [
    () => [h('div', { class: 'fr-btn' }, 'Continue →')],
    () => [h('div', { class: 'fr-lab' }, 'Phone *'), h('div', { class: 'fr-in' })],
    () => [h('div', { class: 'fr-row' }, h('i', { class: 'fr-box' }), 'I agree to the terms')],
    () => [h('div', { class: 'fr-in' }, 'Select…', h('em', null, '▾'))],
    () => [h('div', { class: 'fr-row' }, h('i', { class: 'fr-rad' }), 'Yes', h('i', { class: 'fr-rad' }), 'No')],
    () => [h('div', { class: 'fr-lab' }, 'Are you sure?'), h('div', { class: 'fr-row' }, h('b', null, 'Cancel'), h('b', null, 'OK'))],
    () => [h('div', { class: 'fr-btn ghost' }, 'Upload file')],
    () => [h('div', { class: 'fr-in' }, 'MM / DD / YYYY')],
    () => [h('div', { class: 'fr-row tabs' }, h('b', null, 'Overview'), h('b', null, 'Billing'), h('b', null, 'Settings'))],
    () => [h('div', { class: 'fr-btn' }, 'Next')],
    () => [h('div', { class: 'fr-row' }, h('i', { class: 'fr-tog' }), 'Notifications')],
    () => [h('div', { class: 'fr-lab' }, 'Email *'), h('div', { class: 'fr-in' })],
    () => [h('div', { class: 'fr-btn' }, 'Save')],
    () => [h('div', { class: 'fr-row' }, h('i', { class: 'fr-file' }), 'report-final-v3.xlsx')],
    () => [h('div', { class: 'fr-btn' }, 'Sign in')],
    () => [h('div', { class: 'fr-lab' }, 'Postal code'), h('div', { class: 'fr-in' })],
  ];
  const COLS = 8, ROWS = 20, CW = 250, CH = 132, GX = 300, GY = 178;

  F.scene('cold', {
    push: 0,
    build(root) {
      const style = h('style', null, `
        .plane-wrap{position:absolute;inset:0;perspective:880px;perspective-origin:50% 22%;}
        .plane{position:absolute;left:50%;bottom:-120px;width:${COLS * GX}px;height:${ROWS * GY}px;margin-left:-${(COLS * GX) / 2}px;transform-origin:50% 100%;transform:rotateX(66deg);}
        .plane-in{position:absolute;inset:0;}
        .frag{position:absolute;width:${CW}px;height:${CH}px;border-radius:16px;padding:20px 22px;box-sizing:border-box;
          background:linear-gradient(180deg,rgba(255,255,255,.095),rgba(255,255,255,.035));border:1.5px solid rgba(255,255,255,.16);
          display:flex;flex-direction:column;justify-content:center;gap:10px;font:500 22px/1.2 var(--body);color:#e6e9ef;}
        .frag.done{border-color:rgba(94,230,189,.75);box-shadow:0 0 30px rgba(94,230,189,.3);}
        .frag .ok{position:absolute;right:14px;top:12px;font:700 22px/1 var(--body);color:var(--mint);opacity:0;}
        .fr-btn{align-self:flex-start;padding:12px 18px;border-radius:10px;background:#2b6cf0;color:#fff;font-weight:600;}
        .fr-btn.ghost{background:transparent;border:1.5px solid rgba(255,255,255,.3);}
        .fr-lab{font-size:19px;color:#aab1bd;} .fr-in{height:40px;border-radius:9px;border:1.5px solid rgba(255,255,255,.22);padding:0 12px;font:400 19px/38px var(--body);color:#8b93a1;display:flex;justify-content:space-between;}
        .fr-in em{font-style:normal} .fr-row{display:flex;align-items:center;gap:12px;font-size:20px;}
        .fr-row.tabs b{font-weight:500;font-size:18px;padding-bottom:6px;border-bottom:2px solid transparent} .fr-row.tabs b:first-child{border-color:#2b6cf0}
        .fr-row b{font-weight:600;font-size:18px;padding:8px 12px;border-radius:8px;background:rgba(255,255,255,.08)}
        .fr-box{width:22px;height:22px;border-radius:5px;border:2px solid rgba(255,255,255,.45)} .fr-rad{width:22px;height:22px;border-radius:50%;border:2px solid rgba(255,255,255,.45)}
        .fr-tog{width:42px;height:24px;border-radius:12px;background:rgba(255,255,255,.2)} .fr-file{width:20px;height:26px;border-radius:3px;background:#22c55e}
        .ripple3d{position:absolute;width:160px;height:160px;margin:-80px 0 0 -80px;border-radius:50%;border:4px solid rgba(94,230,189,.95);box-shadow:0 0 40px rgba(94,230,189,.5);}
        .cold-fog{position:absolute;inset:0;background:linear-gradient(180deg,var(--bg) 0%,var(--bg) 16%,rgba(5,6,8,.7) 30%,rgba(5,6,8,0) 55%,rgba(5,6,8,0) 86%,rgba(5,6,8,.55) 100%);}
        .save-card{position:absolute;left:50%;top:50%;width:980px;height:340px;margin:-190px 0 0 -490px;border-radius:24px;background:#ffffff;color:#1f2328;box-shadow:0 60px 160px rgba(0,0,0,.7);overflow:hidden;}
        .save-card .hd{padding:30px 40px 0;font:700 32px/1.2 var(--display);letter-spacing:-.02em}
        .save-card .sub{padding:6px 40px 0;font:400 19px/1.4 var(--body);color:#57606a}
        .save-card .row{position:absolute;left:40px;right:40px;top:136px;display:flex;gap:18px;align-items:center}
        .save-card .in{flex:1;height:52px;border-radius:10px;border:1.5px solid #d0d7de;font:400 21px/50px var(--body);padding:0 16px;color:#1f2328}
        .save-card .btn{padding:16px 24px;border-radius:10px;background:#1f6feb;color:#fff;font:600 20px/1 var(--body)}
        .toast{position:absolute;right:28px;top:24px;display:flex;align-items:center;gap:12px;padding:14px 20px;border-radius:12px;background:#1a7f37;color:#fff;font:600 22px/1 var(--body);box-shadow:0 14px 40px rgba(26,127,55,.35)}
        .truth{position:absolute;left:40px;right:40px;bottom:32px;display:flex;justify-content:space-between;align-items:center;padding:18px 22px;border-radius:12px;background:#fff1f2;border:1.5px solid #ffc9cf;box-shadow:0 0 0 0 rgba(255,108,120,0);font:600 22px/1 var(--mono);color:#b42318}
        .truth span{font:500 19px/1 var(--mono);color:#7a2e2e;letter-spacing:.04em;text-transform:uppercase}
      `);
      root.append(style);
      const wrap = h('div', { class: 'plane-wrap' });
      const plane = h('div', { class: 'plane' });
      const inner = h('div', { class: 'plane-in' });
      plane.append(inner); wrap.append(plane); root.append(wrap);
      const rnd = F.seeded(42);
      const cards = [];
      for (let r = -5; r < ROWS; r++) {
        for (let c = 0; c < COLS; c++) {
          const tpl = FRAG[Math.floor(rnd() * FRAG.length)];
          const el = h('div', { class: 'frag' }, ...tpl(), h('div', { class: 'ok' }, '✓'));
          const x = c * GX + (GX - CW) / 2 + (rnd() - 0.5) * 20;
          const y = r * GY + (GY - CH) / 2;
          el.style.left = x + 'px'; el.style.top = y + 'px';
          inner.append(el);
          cards.push({ el, ok: el.lastChild, x, y, r, c, ph: rnd() });
        }
      }
      // the chain of "easy" clicks: a deterministic walk across near rows
      const chain = [];
      let cc = 3, rr = ROWS - 3;
      const rw = F.seeded(9);
      for (let i = 0; i < 16; i++) {
        chain.push(cards.find((k) => k.r === rr && k.c === cc));
        cc = clamp(cc + (rw() < 0.5 ? -1 : 1) * (1 + Math.floor(rw() * 2)), 0, COLS - 1);
        rr = clamp(rr - (rw() < 0.6 ? 1 : 0), 11, ROWS - 2);
      }
      const ripples = chain.map(() => { const rp = h('div', { class: 'ripple3d' }); inner.append(rp); return rp; });
      root.append(h('div', { class: 'cold-fog' }));
      // focus: the save that lies
      const toast = h('div', { class: 'toast' }, '✓  Saved');
      const truth = h('div', { class: 'truth' }, h('span', null, 'Server record'), 'NOT SAVED');
      const btn = h('div', { class: 'btn' }, 'Save changes');
      const card = h('div', { class: 'save-card' },
        h('div', { class: 'hd' }, 'Contact details'),
        h('div', { class: 'sub' }, 'Update the phone number on file.'),
        h('div', { class: 'row' }, h('div', { class: 'in' }, '(555) 010-0147'), btn), toast, truth);
      const note = h('div', { class: 'abs mono', style: { left: '0', right: '0', top: '940px', textAlign: 'center', font: '500 20px/1.4 var(--mono)', color: '#8b909a', letterSpacing: '.02em' } },
        'Interaction Gauntlet · state_traps: a save that toasts “Saved ✓” and never persists');
      root.append(card, note);
      this.r = { wrap, plane, inner, cards, chain, ripples, card, toast, truth, btn, note };
    },
    update(t, sc) {
      const r = this.r;
      const tAI = WD('c02', 'AI'), tEasy = WE('c02', 'part'), tHard = WD('c03', 'hard'), tKnow = WD('c03', 'knowing');
      const tAct = WD('c03', 'actually'), tHap = WD('c03', 'happened');
      // plane: fade in from black, drift toward the viewer, speed up while the AI clicks
      const fade = P(t, 0.3, 2.4, E.ioS) * (1 - P(t, tKnow - 0.4, 0.9, E.inQ));
      const speed = 34 + 120 * P(t, tAI - 0.3, 1.2, E.ioC) * (1 - P(t, tHard, 1.0, E.outC));
      // integrate speed analytically enough: piecewise
      const base = t * 34;
      const extra = 120 * Math.max(0, Math.min(t, tHard + 0.5) - (tAI + 0.3)) ;
      const y = -160 + base + extra;
      css(r.inner, 'transform', `translate3d(0,${y.toFixed(1)}px,0)`);
      set(r.wrap, { o: fade, blur: P(t, tHard, 1.4, E.inQ) * 6 });
      // ambient pulses during "endless"
      for (const c of r.cards) {
        const k = 0.5 + 0.5 * Math.sin(t * 1.7 + c.ph * 20);
        if (c.__k !== Math.round(k * 20)) { c.__k = Math.round(k * 20); c.el.style.borderColor = `rgba(255,255,255,${(0.08 + 0.1 * k).toFixed(3)})`; }
      }
      // the chain of clicks: accelerating
      const n = r.chain.length;
      const t0 = tAI - 0.05, t1 = tEasy + 0.15;
      r.chain.forEach((c, i) => {
        const at = t0 + (t1 - t0) * Math.pow(i / (n - 1), 0.72);
        const done = t >= at;
        cls(c.el, 'done', done && t < tKnow + 1.5);
        set(c.ok, { o: done ? 1 : 0, s: done ? 1 : 0.6 });
        const rk = clamp((t - at) / 0.7);
        const rp = r.ripples[i];
        css(rp, 'left', (c.x + CW / 2) + 'px'); css(rp, 'top', (c.y + CH / 2) + 'px');
        set(rp, { o: t >= at && rk < 1 ? (1 - rk) : 0, s: 0.3 + 1.4 * E.outC(rk) });
      });
      // focus card
      const kc = P(t, tKnow - 0.15, 0.8, E.outQ5);
      const ko = P(t, sc.end - 0.35, 0.3, E.inQ);
      set(r.card, { o: kc * (1 - ko), s: 0.9 + 0.1 * kc + 0.02 * P(t, tKnow, 3, E.lin), y: (1 - kc) * 40, blur: (1 - kc) * 12 });
      const tClick = tKnow + 0.15;
      set(r.btn, { s: t >= tClick && t < tClick + 0.14 ? 0.95 : 1 });
      const kt = P(t, tClick + 0.25, 0.45, E.outB);
      const flick = t > tHap + 0.15 ? (0.55 + 0.45 * Math.abs(Math.sin(t * 23))) * (1 - 0.6 * P(t, tHap + 0.2, 0.8)) : 1;
      set(r.toast, { o: kt * flick, x: (1 - kt) * 40, sat: 1 - 0.85 * P(t, tHap + 0.2, 0.6) });
      const kr = P(t, tAct - 0.05, 0.5, E.outQ5);
      set(r.truth, { o: kr, y: (1 - kr) * 18 });
      set(r.note, { o: P(t, tHap + 0.3, 0.6) * (1 - ko) * 0.9 });
    },
  });

  // =====================================================================================
  // Scene 2: meet Lumen
  // =====================================================================================
  F.scene('meet', {
    build(root) {
      const style = h('style', null, `
        .lockup{position:absolute;left:0;top:0;width:1920px;height:1080px;}
        .lockup .mk{position:absolute;left:${960 - 380}px;top:${540 - 135}px;}
        .lockup .wm{position:absolute;left:${960 - 380 + 310}px;top:${540 - 108}px;font:600 210px/1 var(--display);letter-spacing:-.055em;color:#f4f4f6;}
        .bloom{position:absolute;left:50%;top:50%;width:1400px;height:1400px;margin:-700px 0 0 -700px;border-radius:50%;background:radial-gradient(circle,rgba(94,230,189,.16),rgba(94,230,189,0) 60%);}
        .callout{position:absolute;display:flex;flex-direction:column;gap:6px;}
        .callout b{font:600 30px/1.1 var(--display);letter-spacing:-.02em;color:#fff}
        .callout span{font:500 18px/1.2 var(--mono);color:var(--blue2);letter-spacing:.02em}
        .stepchip{display:flex;align-items:center;gap:14px;padding:10px 16px;border-radius:12px;background:#121216;border:1px solid #26262b;font:500 19px/1.2 var(--body);color:#e4e4e7}
        .stepchip .ck{width:26px;height:26px;border-radius:50%;display:grid;place-items:center;background:rgba(94,230,189,.14);color:var(--mint);font:700 15px/1 var(--body)}
        .stepchip em{margin-left:auto;font:500 15px/1 var(--mono);font-style:normal;color:var(--mint)}
      `);
      root.append(style);
      const bloom = h('div', { class: 'bloom' });
      root.append(bloom);
      // lockup
      const lk = h('div', { class: 'lockup' });
      const logo = ui.logo(270);
      const mk = h('div', { class: 'mk' }); mk.append(logo.svg);
      const wm = h('div', { class: 'wm' }, 'Lumen');
      lk.append(mk, wm);
      root.append(lk);
      // the screen: desktop + chrome + native window + frame + pill
      const scr = h('div', { class: 'abs', style: { left: '0', top: '0', width: '1920px', height: '1080px' } });
      const D = ui.desktop({ x: 230, y: 190, w: 1460, hh: 820 });
      scr.append(D.el);
      const ch = ui.chrome({ x: 40, y: 84, w: 900, hh: 660, tab: 'Careers · Acme Robotics', url: '<b>jobs.acme.example</b>/careers/4821', avatar: 'JA' });
      const jp = ui.jobPage(ch.page);
      D.layer.append(ch.el);
      const nw = ui.nwin({ x: 900, y: 150, w: 520, hh: 500, title: 'Downloads · File Explorer', menu: ['File', 'Home', 'Share', 'View'] });
      const files = ['resume-jordan-avery.pdf', 'offer-letter-template.docx', 'portfolio-2026.zip', 'headshot.png', 'references.txt'];
      files.forEach((f, i) => nw.body.append(h('div', { style: { display: 'flex', alignItems: 'center', gap: '12px', padding: '12px 18px', font: '400 18px/1.2 "Segoe UI", var(--body)', borderBottom: '1px solid #d6dce4', background: i === 0 ? '#dbe7f5' : 'transparent' } },
        h('i', { style: { width: '18px', height: '22px', borderRadius: '3px', background: ['#e5534b', '#2f6fdb', '#d4a72c', '#2da44e', '#8c959f'][i], display: 'block' } }), f)));
      D.layer.append(nw.el);
      const frame = h('div', { class: 'frame' });
      D.el.append(frame);
      const pillWrap = h('div', { class: 'pillwrap', style: { top: '18px' } });
      D.el.append(pillWrap);
      const pill = F.Pill(pillWrap);
      root.append(scr);
      const c1 = h('div', { class: 'callout', style: { left: '250px', top: '120px' } }, h('b', null, 'Your Chrome'), h('span', null, 'your profile · your signed-in sessions'));
      const c2 = h('div', { class: 'callout', style: { left: '1240px', top: '120px', alignItems: 'flex-end', textAlign: 'right' } }, h('b', null, 'Your Windows desktop'), h('span', null, 'native apps · files · dialogs'));
      root.append(c1, c2);
      // cockpit: composer + steps
      const cockpit = h('div', { class: 'cockpit', style: { left: '1110px', top: '130px', width: '700px', height: '860px' } });
      const head = h('div', { style: { display: 'flex', alignItems: 'center', gap: '14px', padding: '22px 26px', borderBottom: '1px solid #1f1f24' } },
        h('div', { style: { width: '40px', height: '40px', borderRadius: '11px', background: '#7c5cff', boxShadow: '0 0 24px rgba(124,92,255,.45)', display: 'grid', placeItems: 'center' } }, F.icon('sparkles', 22, '#ffffff')),
        h('div', null, h('div', { style: { font: '600 22px/1.1 var(--display)', color: '#f4f4f6' } }, 'Lumen'), h('div', { style: { font: '400 15px/1.3 var(--body)', color: '#a1a1aa' } }, 'Personal task agent')));
      const bubble = h('div', { class: 'bubble-user abs', style: { right: '26px', top: '120px', maxWidth: '520px' } }, 'Apply to this job: jobs.acme.example/careers/4821');
      const reply = h('div', { class: 'abs', style: { left: '26px', top: '230px', right: '26px', display: 'flex', gap: '14px', alignItems: 'flex-start' } },
        h('div', { class: 'spark' }, F.icon('sparkles', 18, '#a996ff')),
        h('div', { style: { font: '400 21px/1.45 var(--body)', color: '#e4e4e7', paddingTop: '4px' } }, 'Got it — I started a live application session on jobs.acme.example. I’m working through the form now and will post updates here as I go.'));
      const steps = h('div', { class: 'abs', style: { left: '74px', right: '26px', top: '350px', display: 'flex', flexDirection: 'column', gap: '8px' } });
      const stepTexts = [['Opened jobs.acme.example', 'verified'], ['First name ← profile', 'verified'], ['Last name ← profile', 'verified'], ['Email ← profile', 'verified']];
      const stepEls = stepTexts.map(([a, b]) => { const e = h('div', { class: 'stepchip' }, h('div', { class: 'ck' }, '✓'), a, h('em', null, b)); steps.append(e); return e; });
      const comp = h('div', { class: 'composer abs', style: { left: '26px', right: '26px', bottom: '26px', height: '96px' } });
      const compText = h('div', { style: { font: '400 21px/1.4 var(--body)', color: '#f4f4f6' } });
      const compPh = h('div', { class: 'abs', style: { left: '18px', top: '16px', font: '400 21px/1.4 var(--body)', color: '#71717a' } }, 'Message Lumen…');
      const modeChip = h('div', { class: 'abs', style: { left: '18px', bottom: '14px', font: '500 16px/1 var(--body)', color: '#a1a1aa', display: 'flex', gap: '8px', alignItems: 'center' } }, F.icon('shield', 18, '#a1a1aa'), 'Safe approvals');
      const send = h('div', { class: 'abs', style: { right: '14px', bottom: '14px', width: '40px', height: '40px', borderRadius: '10px', background: '#7c5cff', color: '#fff', display: 'grid', placeItems: 'center', font: '700 20px/1 var(--body)' } }, '↑');
      comp.append(compPh, compText, modeChip, send);
      cockpit.append(head, bubble, reply, steps, comp);
      root.append(cockpit);
      this.r = { bloom, lk, logo, mk, wm, scr, D, ch, jp, nw, frame, pill, c1, c2, cockpit, bubble, reply, stepEls, compText, compPh };
    },
    update(t, sc) {
      const r = this.r;
      const tLumen = WD('m01', 'Lumen'), tA = LN('m02').start, tChrome = WD('m02', 'Chrome'), tSigned = WD('m02', 'signed'),
        tWin = WD('m02', 'Windows'), tYou = LN('m03').start, tOut = WD('m03', 'outcome'), tGets = WD('m03', 'Lumen'),
        tVer = WD('m03', 'verified'), tStep = WD('m03', 'step');
      const end = sc.end;
      const fadeOut = 1 - P(t, end - 0.45, 0.45, E.inQ);
      // bloom + logo assembly
      set(r.bloom, { o: env(t, 15.4, tA + 1.0, 1.6, 1.4) * 0.9, s: 0.7 + 0.3 * P(t, 15.4, 3, E.outC) });
      set(r.logo.tile, {}); // tile static inside svg
      const kTile = P(t, 15.35, 1.0, E.outQ5);
      set(r.mk, { o: kTile, s: 0.92 + 0.08 * kTile });
      r.logo.slabs.forEach((sl, i) => {
        const k = P(t, 15.75 + i * 0.13, 0.85, E.outB);
        sl.setAttribute('transform', `translate(0 ${((1 - k) * 760).toFixed(1)})`);
      });
      const sw = P(t, tLumen - 0.1, 1.2, E.ioC);
      r.logo.sweep.setAttribute('x', (-500 + sw * 1700).toFixed(1));
      const kW = P(t, tLumen - 0.05, 0.9, E.outQ5);
      set(r.wm, { o: kW, x: (1 - kW) * -30, blur: (1 - kW) * 14 });
      // lockup moves up and shrinks into a header as the screen arrives
      const kUp = P(t, tA - 0.35, 1.1, E.ioE);
      set(r.lk, { o: 1 - P(t, tA - 0.3, 0.7, E.inQ), y: lerp(0, -40, kUp), s: lerp(1, 0.86, kUp), blur: kUp * 8 });
      r.lk.style.transformOrigin = '960px 540px';
      // screen composition
      const kS = P(t, tA - 0.2, 1.1, E.outQ5);
      const kCockpit = P(t, tYou - 0.4, 1.0, E.ioE);
      set(r.scr, { o: kS * fadeOut, y: (1 - kS) * 60, s: lerp(1, 0.66, kCockpit), x: lerp(0, -408, kCockpit) });
      r.scr.style.transformOrigin = '960px 600px';
      set(r.frame, { o: P(t, tA + 0.4, 0.5) });
      r.pill.update(t, P(t, tA + 0.6, 0.5), [[tA + 0.6, 'Lumen is using your computer']]);
      // highlights
      const hiC = env(t, tChrome - 0.1, tWin - 0.1, 0.4, 0.4);
      css(r.ch.el, 'box-shadow', `0 40px 120px rgba(0,0,0,.62), 0 0 0 ${(2 * hiC).toFixed(2)}px rgba(85,173,255,${(0.9 * hiC).toFixed(2)}), 0 0 ${(60 * hiC).toFixed(1)}px rgba(85,173,255,${(0.35 * hiC).toFixed(2)})`);
      const hiA = env(t, tSigned - 0.05, tWin - 0.1, 0.3, 0.4);
      css(r.ch.av, 'box-shadow', `0 0 0 ${(4 * hiA).toFixed(1)}px rgba(255,255,255,${(0.9 * hiA).toFixed(2)}), 0 0 ${(26 * hiA).toFixed(1)}px rgba(124,92,255,${hiA.toFixed(2)})`);
      const hiW = env(t, tWin - 0.1, tYou, 0.4, 0.4);
      css(r.nw.el, 'box-shadow', `0 30px 90px rgba(0,0,0,.55), 0 0 0 ${(2 * hiW).toFixed(2)}px rgba(85,173,255,${(0.9 * hiW).toFixed(2)}), 0 0 ${(60 * hiW).toFixed(1)}px rgba(85,173,255,${(0.35 * hiW).toFixed(2)})`);
      const k1 = env(t, tChrome - 0.15, tYou - 0.3, 0.5, 0.5);
      set(r.c1, { o: k1, y: (1 - P(t, tChrome - 0.15, 0.6, E.outQ5)) * 16 });
      const k2 = env(t, tWin - 0.15, tYou - 0.3, 0.5, 0.5);
      set(r.c2, { o: k2, y: (1 - P(t, tWin - 0.15, 0.6, E.outQ5)) * 16 });
      // cockpit
      set(r.cockpit, { o: kCockpit * fadeOut, x: (1 - kCockpit) * 80 });
      const msg = 'Apply to this job: jobs.acme.example/careers/4821';
      const typing = t >= tYou + 0.2 && t < tOut + 0.55;
      if (typing || t < tYou + 0.2) {
        const n = clamp(Math.floor((t - (tYou + 0.2)) * 62), 0, msg.length);
        text(r.compText, msg.slice(0, n)); set(r.compPh, { o: n === 0 ? 1 : 0 }); set(r.compText, { o: 1 });
      } else { set(r.compText, { o: 0 }); set(r.compPh, { o: 1 }); }
      const kB = P(t, tOut + 0.6, 0.5, E.outQ5);
      set(r.bubble, { o: kB, y: (1 - kB) * 16 });
      const kR = P(t, tGets - 0.1, 0.5, E.outQ5);
      set(r.reply, { o: kR, y: (1 - kR) * 12 });
      r.stepEls.forEach((e, i) => {
        const k = P(t, tVer - 0.25 + i * 0.36, 0.45, E.outQ5);
        set(e, { o: k, y: (1 - k) * 14 });
      });
    },
  });
})();
