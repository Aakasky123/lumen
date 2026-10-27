/* Lumen launch film: scenes 5-7 (receipts, grounded or asked, the floors). */
(function () {
  'use strict';
  const F = window.FILM;
  const { E, P, env, clamp, lerp, h, s, set, css, text, cls, WD, WE, LN, icon, pos } = F;
  const ui = F.ui;

  const sampleTag = () => h('div', { class: 'abs', style: { right: '72px', top: '58px', font: '500 16px/1 var(--mono)', letterSpacing: '.12em', textTransform: 'uppercase', color: '#5b616b' } }, 'dramatization · sample data');

  // =====================================================================================
  // Scene 5: receipts
  // =====================================================================================
  F.scene('receipts', {
    build(root) {
      root.append(h('style', null, `
        .rcpt{position:absolute;border-radius:18px;background:#0c0e12;border:1px solid rgba(255,255,255,.1);box-shadow:0 40px 120px rgba(0,0,0,.6);font-family:var(--mono);overflow:hidden}
        .rcpt .top{height:14px;background:repeating-linear-gradient(90deg,transparent 0 14px,rgba(255,255,255,.06) 14px 16px)}
        .rcpt .ttl{display:flex;justify-content:space-between;padding:22px 30px 6px;font:600 19px/1 var(--mono);letter-spacing:.16em;text-transform:uppercase;color:#8b909a}
        .rrow{display:grid;grid-template-columns:220px 1fr 40px;align-items:center;padding:13px 30px;font:400 24px/1.2 var(--mono);color:#c7cad1}
        .rrow .k{color:#6b7280}
        .rrow .st{justify-self:end;width:18px;height:18px;border-radius:50%;background:#2a2e35}
        .rrow.on .st{background:var(--mint);box-shadow:0 0 16px rgba(94,230,189,.8)}
        .rrow.on .v{color:#eafff7}
        .rule{margin:10px 30px;border-top:1px dashed rgba(255,255,255,.18)}
        .outc{padding:16px 30px 26px;display:flex;align-items:baseline;gap:22px}
        .outc .k{font:400 24px/1 var(--mono);color:#6b7280;width:198px}
        .outc .v{font:600 34px/1 var(--mono);color:var(--mint);letter-spacing:-.01em}
        .fnbox{position:absolute;padding:22px 26px;border-radius:16px;background:rgba(94,230,189,.06);border:1.5px solid rgba(94,230,189,.45);font:500 24px/1.4 var(--mono);color:#d1fae5}
        .ghost{position:absolute;padding:16px 20px;border-radius:14px;background:#0b0d11;border:1.5px dashed rgba(255,108,120,.6);font:500 24px/1.3 var(--mono);color:#fca5ae}
        .mini{position:absolute;width:760px;border-radius:18px;background:#0c0e12;border:1px solid rgba(255,255,255,.1);box-shadow:0 40px 120px rgba(0,0,0,.6);padding:26px 30px;box-sizing:border-box}
        .mini .hd{font:600 30px/1.2 var(--display);letter-spacing:-.02em;color:#fff;display:flex;gap:14px;align-items:center}
        .mini .ln{font:400 21px/1.75 var(--mono);color:#9ca3af;margin-top:12px;white-space:pre}
        .mini .ln b{font-weight:500;color:#e5e7eb}
      `));
      root.append(sampleTag());
      // main receipt
      const rows = [
        ['operation_id', 'fill:input#phone'],
        ['type', 'field_filled'],
        ['target', '"Phone"'],
        ['attempt', 'attempted'],
        ['execution', 'succeeded'],
        ['verification', 'succeeded · read-back matched'],
      ];
      const rc = h('div', { class: 'rcpt', style: { left: '170px', top: '190px', width: '860px' } }, h('div', { class: 'top' }),
        h('div', { class: 'ttl' }, h('span', null, 'Operation receipt'), h('span', null, 'step 8')));
      const rowEls = rows.map(([k, v], i) => { const el = h('div', { class: 'rrow' }, h('span', { class: 'k' }, k), h('span', { class: 'v' }, v), h('i', { class: 'st', style: i < 3 ? { visibility: 'hidden' } : null })); rc.append(el); return el; });
      rc.append(h('div', { class: 'rule' }));
      const outV = h('span', { class: 'v' }, 'verified_succeeded');
      const outc = h('div', { class: 'outc' }, h('span', { class: 'k' }, 'outcome'), outV);
      rc.append(outc);
      root.append(rc);
      // compute_outcome box + ghost claim
      const fn = h('div', { class: 'fnbox', style: { left: '1110px', top: '360px', width: '640px' } },
        h('div', { style: { font: '500 17px/1 var(--mono)', letterSpacing: '.14em', color: '#5ee6bd', marginBottom: '14px' } }, 'RUNTIME'),
        'compute_outcome(', h('br'), '  attempt, execution,', h('br'), '  verification)', h('br'), '→ verified_succeeded');
      const ghost = h('div', { class: 'ghost', style: { left: '1110px', top: '680px', width: '640px' } },
        h('div', { style: { font: '500 17px/1 var(--mono)', letterSpacing: '.14em', color: '#fca5ae', marginBottom: '12px' } }, 'FROM THE MODEL'),
        '"outcome": "success"');
      const ghostX = h('div', { class: 'abs', style: { left: '1110px', top: '800px', width: '640px', display: 'flex', alignItems: 'center', gap: '12px', font: '500 20px/1.3 var(--mono)', color: '#ff8a95' } },
        icon('lock', 22, '#ff8a95'), 'never accepted: outcomes are never planner-supplied');
      const svg = s('svg', { width: 1920, height: 1080, style: 'position:absolute;left:0;top:0' });
      const link = s('path', { d: 'M 1032 560 C 1070 560, 1070 470, 1106 470', fill: 'none', stroke: '#5ee6bd', 'stroke-width': 3 });
      svg.append(link);
      root.append(svg, fn, ghost, ghostX);
      // phase B: two cases
      const m1 = h('div', { class: 'mini', style: { left: '170px', top: '330px' } },
        h('div', { class: 'hd' }, icon('browser', 28, '#f6aa28'), 'Click opens a menu'),
        h('div', { class: 'ln', html: 'type          <b>browser_click</b>\nverification  <b>succeeded</b>\noutcome       <b>verified_succeeded</b>\nprogress      <b style="color:#f6aa28">transient_surface_change</b>' }),
        h('div', { class: 'tag warn', style: { marginTop: '18px' } }, 'verified, but not progress'));
      const m2 = h('div', { class: 'mini', style: { left: '990px', top: '330px' } },
        h('div', { class: 'hd' }, icon('eye', 28, '#8ec8ff'), 'Outcome uncertain'),
        h('div', { class: 'ln', html: 'final click   <b>1 activation</b>\nfirst check   <b style="color:#8ec8ff">inconclusive</b>\nnext step     <b>observe again, no second click</b>\nreconciled    <b style="color:#5ee6bd">COMPLETED</b>' }),
        h('div', { class: 'tag info', style: { marginTop: '18px' } }, 'real: Ashby application, run 493'));
      const twice = h('div', { class: 'abs', style: { left: '990px', top: '720px', width: '760px', display: 'flex', alignItems: 'center', gap: '18px', font: '600 30px/1 var(--display)', color: '#fff' } },
        h('span', { style: { font: '500 20px/1 var(--mono)', color: '#8b909a', letterSpacing: '.1em' } }, 'ACTIVATIONS'),
        h('span', { style: { color: '#5ee6bd' } }, '1'), h('span', { style: { position: 'relative', color: '#6b7280' } }, '2', h('i', { style: { position: 'absolute', left: '-6px', right: '-6px', top: '50%', height: '3px', background: '#ff6c78' } })));
      root.append(m1, m2, twice);
      this.r = { rc, rowEls, outc, outV, fn, ghost, ghostX, link, m1, m2, twice };
    },
    update(t, sc) {
      const r = this.r;
      const tRec = WD('r01', 'receipt'), tAtt = WD('r01', 'attempted'), tExe = WD('r01', 'executed'), tVer = WD('r01', 'verified');
      const tRun = WD('r01', 'runtime'), tOut = WD('r01', 'outcome'), tModel = WD('r01', 'model'), tNever = WD('r01', 'never'), tWork = WE('r01', 'work');
      const tOpen = WD('r02', 'Opening'), tUnc = WD('r02', 'uncertain'), tLooks = WD('r02', 'looks'), tTwice = WD('r02', 'twice');
      const out = 1 - P(t, sc.end - 0.4, 0.45, E.inQ);
      const phaseA = 1 - P(t, tOpen - 0.45, 0.45, E.inQ);
      const kR = P(t, sc.start + 0.1, 0.9, E.outQ5);
      set(r.rc, { o: kR * phaseA, y: (1 - kR) * 60 });
      r.rowEls.forEach((el, i) => {
        const at = i < 3 ? tRec - 0.2 + i * 0.12 : [tAtt, tExe, tVer][i - 3];
        const k = P(t, at - 0.05, 0.35, E.outQ5);
        set(el, { o: 0.35 + 0.65 * k, x: (1 - k) * -10 });
        cls(el, 'on', i >= 3 && t >= at);
      });
      const kO = P(t, tOut - 0.1, 0.5, E.outQ5);
      set(r.outc, { o: kO, x: (1 - kO) * -14 });
      const kF = P(t, tRun - 0.2, 0.6, E.outQ5);
      set(r.fn, { o: kF * phaseA, x: (1 - kF) * 30 });
      F.drawPath(r.link, P(t, tRun, 0.6));
      set(r.link, { o: phaseA });
      const kG = P(t, tModel - 0.1, 0.5, E.outQ5);
      const shake = t > tNever && t < tNever + 0.35 ? Math.sin((t - tNever) * 90) * 7 * (1 - (t - tNever) / 0.35) : 0;
      set(r.ghost, { o: kG * phaseA * (1 - 0.55 * P(t, tWork - 0.4, 0.4)), x: shake, y: (1 - kG) * -16 });
      set(r.ghostX, { o: P(t, tNever, 0.4) * phaseA });
      // phase B
      const k1 = P(t, tOpen - 0.2, 0.6, E.outQ5);
      set(r.m1, { o: k1 * out, y: (1 - k1) * 40 });
      const k2 = P(t, tUnc - 0.3, 0.6, E.outQ5);
      set(r.m2, { o: k2 * out, y: (1 - k2) * 40 });
      const kT = P(t, tTwice - 0.5, 0.5, E.outQ5);
      set(r.twice, { o: kT * out, y: (1 - kT) * 14 });
    },
  });

  // =====================================================================================
  // Scene 6: grounded, or asked (and remembered)
  // =====================================================================================
  F.scene('ground', {
    build(root) {
      root.append(h('style', null, `
        .ghostval{position:absolute;padding:0 14px;height:46px;font:400 19px/43px var(--body);color:#9a6700;border:2px dashed rgba(207,34,46,.7);border-radius:8px;background:rgba(255,235,235,.6)}
        .cite{position:absolute;display:inline-flex;align-items:center;gap:10px;padding:8px 12px;border-radius:9px;font:500 16px/1 var(--mono);white-space:nowrap}
        .cite.bad{background:#fff1f2;color:#b42318;border:1.5px solid #ffc9cf}
        .cite.ok{background:#ecfdf5;color:#047857;border:1.5px solid #a7f3d0}
        .cite.gray{background:#f6f8fa;color:#57606a;border:1.5px solid #d0d7de}
        .memfile{position:absolute;border-radius:16px;background:#0c0e12;border:1px solid rgba(255,255,255,.12);box-shadow:0 40px 120px rgba(0,0,0,.65);overflow:hidden}
        .memfile .bar{display:flex;align-items:center;gap:12px;padding:14px 20px;background:#14161b;border-bottom:1px solid rgba(255,255,255,.07);font:500 18px/1 var(--mono);color:#c7cad1}
        .memfile .body{padding:18px 22px;font:400 19px/1.75 var(--mono);color:#9ca3af;white-space:pre}
        .memfile .body .hd{color:#e5e7eb;font-weight:500}
        .memfile .body .nw{color:#d1fae5;background:rgba(94,230,189,.1);box-shadow:0 0 0 1.5px rgba(94,230,189,.55);border-radius:6px;padding:2px 6px;margin-left:-6px}
      `));
      root.append(sampleTag());
      const ch = ui.chrome({ x: 60, y: 120, w: 1000, hh: 920, tab: 'Careers · Acme Robotics', url: '<b>jobs.acme.example</b>/careers/4821/apply', avatar: 'JA' });
      const jp = ui.jobPage(ch.page);
      root.append(ch.el);
      text(jp.fields.phone.val, '(555) 010-0147');
      text(jp.fields.resume.val, 'resume-jordan-avery.pdf');
      jp.fields.resume.val.className = '';
      text(jp.fields.auth.val, 'Yes'); jp.fields.auth.val.className = '';
      // cockpit panel
      const ck = h('div', { class: 'cockpit', style: { left: '1110px', top: '120px', width: '750px', height: '920px' } });
      const qcard = h('div', { class: 'qcard abs', style: { left: '26px', right: '26px', top: '40px' } },
        h('div', { style: { display: 'flex', alignItems: 'center', gap: '10px', font: '600 18px/1 var(--body)', color: '#fcd34d' } }, icon('alert', 20, '#fcd34d'), 'Lumen needs your input'),
        h('div', { style: { font: '600 26px/1.3 var(--display)', letterSpacing: '-.01em', color: '#fff', marginTop: '16px' } }, 'How many years have you used Python in production?'),
        h('div', { style: { display: 'flex', gap: '10px', marginTop: '14px' } }, h('span', { class: 'tag warn' }, 'required'), h('span', { class: 'tag' }, 'no grounded answer found')),
        h('div', { style: { font: '400 19px/1.4 var(--body)', color: '#a1a1aa', marginTop: '16px' } }, 'Reply below to continue.'));
      const ubub = h('div', { class: 'bubble-user abs', style: { right: '26px', top: '330px', maxWidth: '520px' } }, '4 years. Remember that for next time.');
      const reply = h('div', { class: 'abs', style: { left: '26px', right: '26px', top: '430px', display: 'flex', gap: '14px', alignItems: 'flex-start' } },
        h('div', { class: 'spark' }, icon('sparkles', 18, '#a996ff')),
        h('div', { style: { font: '400 21px/1.45 var(--body)', color: '#e4e4e7', paddingTop: '4px' } }, 'Saved - I’ll remember python production years: 4 years. Everything I know about you lives in one memory file you can view or edit any time.'));
      const comp = h('div', { class: 'composer abs', style: { left: '26px', right: '26px', bottom: '26px', height: '96px' } },
        h('div', { style: { font: '400 21px/1.4 var(--body)', color: '#71717a' } }, 'Message Lumen…'));
      ck.append(qcard, ubub, reply, comp);
      root.append(ck);
      // memory file
      const memNew = h('span', { class: 'nw' }, '- python_production_years: 4 years <!-- 2026-10-01, run 1549 -->');
      const mem = h('div', { class: 'memfile', style: { left: '1096px', top: '690px', width: '790px' } },
        h('div', { class: 'bar' }, icon('file', 20, '#8b909a'), 'memory / user-1.md', h('span', { style: { marginLeft: 'auto', color: '#5b616b' } }, 'plain markdown · editable')),
        h('div', { class: 'body' },
          h('span', { class: 'hd' }, '# What Lumen knows about you'), '\n',
          '- phone: (555) 010-0147 <!-- 2026-09-12, run 1388 -->\n',
          '- work_authorization: authorized to work in the US <!-- … -->\n',
          '- requires_sponsorship: No <!-- 2026-09-12, run 1388 -->\n',
          memNew));
      root.append(mem);
      this.r = { ch, jp, ck, qcard, ubub, reply, mem, memNew, scrolled: false };
    },
    _layout() {
      const r = this.r;
      const page = r.ch.page;
      const f = r.jp.fields;
      const py = pos(f.python.inp, page);
      const pr = pos(f.pronouns.inp, page);
      r.py = py; r.pr = pr;
      r.ghost = h('div', { class: 'ghostval', style: { left: py.x + 'px', top: py.y + 'px', width: (py.w - 28) + 'px' } }, '6');
      const rightOf = (p) => (page.offsetWidth - (p.x + p.w) + 8) + 'px';
      r.citeBad = h('div', { class: 'cite bad', style: { right: rightOf(py), top: (py.y + 7) + 'px' } }, icon('x', 16, '#b42318', 2.4), 'resume: "6 years of Python" not found: rejected before typing');
      r.citeOk = h('div', { class: 'cite ok', style: { right: rightOf(py), top: (py.y + 7) + 'px' } }, icon('check', 16, '#047857', 2.4), 'human_answer: "4 years"');
      r.skip = h('div', { class: 'cite gray', style: { right: rightOf(pr), top: (pr.y + 7) + 'px' } }, 'optional, no grounded answer: left unchanged');
      for (const el of [r.ghost, r.citeBad, r.citeOk, r.skip]) page.append(el);
      r.scrolled = true;
    },
    update(t, sc) {
      const r = this.r;
      if (!r.scrolled) this._layout();
      const tGuess = WD('g01', 'guess'), tFacts = WD('g01', 'facts'), tReq = WD('g02', 'required'), tComes = WD('g02', 'comes'), tOpt = WD('g02', 'Optional'), tAlone = WD('g02', 'alone');
      const tTell = WD('g03', 'Tell'), tRem = WD('g03', 'remember'), tAns = WD('g03', 'answer'), tMem = WD('g03', 'memory'), tEdit = WD('g03', 'edit');
      const out = 1 - P(t, sc.end - 0.4, 0.45, E.inQ);
      const kIn = P(t, sc.start - 0.2, 0.9, E.outQ5);
      set(r.ch.el, { o: kIn * out });
      // scroll the page so the screening questions are in view
      const sy = -300 * P(t, sc.start, 1.2, E.ioC);
      set(r.jp.wrap, { y: sy });
      for (const el of [r.ghost, r.citeBad, r.citeOk, r.skip]) css(el, 'transform', `translateY(${sy.toFixed(1)}px)`);
      // the fabricated value is rejected
      const kg = P(t, tGuess - 0.3, 0.4, E.outQ5);
      const kgx = P(t, tReq - 0.2, 0.4, E.inQ);
      set(r.ghost, { o: kg * (1 - kgx) });
      set(r.citeBad, { o: P(t, tFacts - 0.05, 0.35) * (1 - kgx) });
      // question goes to the user; field waits
      const waiting = t > tComes - 0.2 && t < tAns;
      css(r.jp.fields.python.inp, 'border-color', waiting ? '#7c5cff' : (t >= tAns ? '#2da44e' : '#d0d7de'));
      css(r.jp.fields.python.inp, 'box-shadow', waiting ? '0 0 0 4px rgba(124,92,255,.18)' : 'none');
      const kc = P(t, sc.start + 0.2, 0.8, E.outQ5);
      set(r.ck, { o: kc * out, x: (1 - kc) * 40 });
      const kq = P(t, tComes - 0.35, 0.6, E.outQ5);
      set(r.qcard, { o: kq, y: (1 - kq) * 20 });
      set(r.skip, { o: P(t, tOpt, 0.4) });
      // the answer, remembered
      const kb = P(t, tTell + 0.1, 0.5, E.outQ5);
      set(r.ubub, { o: kb, y: (1 - kb) * 14 });
      const filled = t >= tAns;
      text(r.jp.fields.python.val, filled ? '4' : '');
      set(r.citeOk, { o: P(t, tAns, 0.35) });
      const kr = P(t, tAns + 0.3, 0.5, E.outQ5);
      set(r.reply, { o: kr, y: (1 - kr) * 12 });
      const km = P(t, tMem - 0.3, 0.7, E.outQ5);
      set(r.mem, { o: km * out, y: (1 - km) * 80 });
      const kn = P(t, tMem + 0.3, 0.5);
      css(r.memNew, 'opacity', kn.toFixed(3));
      css(r.memNew, 'box-shadow', `0 0 0 1.5px rgba(94,230,189,${(0.55 * kn).toFixed(2)}), 0 0 ${(24 * env(t, tMem + 0.3, tEdit + 0.8, 0.4, 0.6)).toFixed(1)}px rgba(94,230,189,.45)`);
    },
  });

  // =====================================================================================
  // Scene 7: the floors
  // =====================================================================================
  const VOCAB = ['click', 'type', 'use_credential', 'scroll', 'key', 'drag', 'hover', 'back', 'forward', 'navigate', 'reload', 'switch_tab',
    'zoom', 'upload', 'launch_app', 'focus_window', 'switch_surface', 'wait', 'watch', 'query_candidate', 'ask_user', 'done'];
  F.scene('floors', {
    build(root) {
      root.append(h('style', null, `
        .fl-title{position:absolute;left:0;right:0;text-align:center}
        .floorbar{position:absolute;left:560px;width:800px;top:640px;height:10px;border-radius:5px;background:linear-gradient(90deg,rgba(94,230,189,0),#5ee6bd 20%,#5ee6bd 80%,rgba(94,230,189,0));box-shadow:0 0 40px rgba(94,230,189,.5)}
        .vocab{position:absolute;display:grid;grid-template-columns:repeat(4,auto);gap:10px 12px}
        .vocab span{font:500 18px/1 var(--mono);color:#9ca3af;padding:9px 12px;border-radius:8px;background:rgba(255,255,255,.035);border:1px solid rgba(255,255,255,.08)}
        .judge{position:absolute;border-radius:18px;background:#0c0e12;border:1px solid rgba(255,255,255,.1);box-shadow:0 40px 120px rgba(0,0,0,.6);padding:24px 28px;box-sizing:border-box}
        .judge .k{font:500 17px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase;color:#8b909a}
        .meter{position:relative;height:16px;border-radius:8px;background:linear-gradient(90deg,#3a1f24,#3a3220 60%,#123a2e);margin-top:16px}
        .meter i{position:absolute;top:-10px;width:3px;height:36px;background:#fff;border-radius:2px;box-shadow:0 0 12px #fff}
        .meter b{position:absolute;top:-30px;right:15%;margin-right:-1px;white-space:nowrap;font:500 16px/1 var(--mono);color:#5ee6bd}
        .meter u{position:absolute;top:-6px;left:85%;width:2px;height:28px;background:#5ee6bd}
        .split{position:absolute;top:210px;width:780px;height:620px;border-radius:18px;overflow:hidden;background:#fff;box-shadow:0 40px 120px rgba(0,0,0,.6)}
        .split .cap{position:absolute;left:0;right:0;top:-46px;text-align:center;font:500 19px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase;color:#8b909a}
        .sign{position:absolute;left:70px;right:70px;top:70px}
        .sign .t{font:700 34px/1.2 var(--display);letter-spacing:-.02em;color:#1f2328}
        .sign .f{margin-top:28px}
        .sign .l{font:600 18px/1.2 var(--body);color:#1f2328;margin-bottom:8px}
        .sign .in{height:54px;border:1.5px solid #d0d7de;border-radius:9px;font:400 22px/51px var(--body);color:#1f2328;padding:0 16px;position:relative;overflow:hidden}
        .sign .mask{position:absolute;inset:-2px;background:#111111}
        .sign .btn{margin-top:30px;display:inline-block;padding:16px 26px;border-radius:10px;background:#1f6feb;color:#fff;font:600 20px/1 var(--body)}
        .blocked{position:absolute;border-radius:16px;background:#0c0e12;border:1px solid rgba(255,255,255,.1);padding:20px 24px;box-sizing:border-box;box-shadow:0 30px 90px rgba(0,0,0,.6)}
        .blocked .row{display:flex;align-items:center;gap:14px;padding:9px 0;font:500 21px/1.2 var(--body);color:#9ca3af}
        .blocked .row s{color:#6b7280;text-decoration-color:#ff6c78;text-decoration-thickness:2px}
        .hostbar{position:absolute;border-radius:16px;background:#121216;border:1px solid #26262b;padding:20px 22px;box-sizing:border-box;box-shadow:0 30px 90px rgba(0,0,0,.6);font-family:var(--body)}
        .hostbar .r{display:flex;justify-content:space-between;align-items:center;padding:10px 0;font:500 20px/1 var(--body);color:#e4e4e7;border-bottom:1px solid #1f1f24}
        .hostbar .r span{font:500 19px/1 var(--body)}
        .hostbar .pbtn{margin-top:16px;display:inline-flex;align-items:center;gap:10px;padding:12px 16px;border-radius:12px;background:#1d1d20;border:1px solid #2b2b31;font:600 19px/1 var(--body);color:#f4f4f6}
      `));
      // f01 title
      const t1 = h('div', { class: 'fl-title h2', style: { top: '380px' } }, 'Some lines aren’t settings.');
      const t2 = h('div', { class: 'fl-title h1 mint', style: { top: '486px' } }, 'They’re floors.');
      const bar = h('div', { class: 'floorbar' });
      root.append(t1, t2, bar);
      // f02: page bottom + vocabulary + judge
      const stage2 = h('div', { class: 'abs', style: { inset: '0' } });
      const ch = ui.chrome({ x: 60, y: 120, w: 980, hh: 920, tab: 'Careers · Acme Robotics', url: '<b>jobs.acme.example</b>/careers/4821/apply', avatar: 'JA' });
      const jp = ui.jobPage(ch.page);
      for (const [k, v] of [['phone', '(555) 010-0147'], ['resume', 'resume-jordan-avery.pdf'], ['auth', 'Yes'], ['python', '4']]) { text(jp.fields[k].val, v); jp.fields[k].val.className = ''; }
      set(jp.wrap, { y: -380 });
      const thanks = h('div', { class: 'abs', style: { inset: '0', background: '#fff', display: 'grid', placeItems: 'center' } },
        h('div', { style: { textAlign: 'center' } },
          h('div', { style: { width: '84px', height: '84px', margin: '0 auto', borderRadius: '50%', background: '#dafbe1', display: 'grid', placeItems: 'center' } }, icon('check', 46, '#1a7f37', 3)),
          h('div', { style: { font: '700 44px/1.2 var(--display)', letterSpacing: '-.02em', color: '#1f2328', marginTop: '26px' } }, 'Thanks for applying!'),
          h('div', { style: { font: '400 21px/1.5 var(--body)', color: '#57606a', marginTop: '10px' } }, 'Acme Robotics has received your application.')));
      ch.page.append(thanks);
      const pointer = F.Pointer(ch.page);
      stage2.append(ch.el);
      const vocab = h('div', { class: 'vocab', style: { left: '1100px', top: '200px' } }, ...VOCAB.map((v) => h('span', null, v)));
      const vocabCap = h('div', { class: 'abs', style: { left: '1100px', top: '150px', font: '500 17px/1 var(--mono)', letterSpacing: '.14em', color: '#8b909a' } }, 'EVERYTHING THE MODEL CAN EMIT');
      const noAuth = h('div', { class: 'abs', style: { left: '1100px', top: '520px', width: '760px', display: 'flex', alignItems: 'center', gap: '14px', font: '500 23px/1.35 var(--mono)', color: '#ff8a95' } },
        icon('lock', 26, '#ff8a95'), 'no approve, no allow_final_submit:\nfinal authority is backend-only');
      noAuth.style.whiteSpace = 'pre-line';
      const judge = h('div', { class: 'judge', style: { left: '1100px', top: '640px', width: '760px', height: '296px' } },
        h('div', { class: 'k' }, 'Click-consequence judge · before every click'),
        h('div', { style: { font: '600 32px/1.2 var(--display)', letterSpacing: '-.02em', color: '#fff', marginTop: '16px' } }, 'click → "Submit application"'),
        h('div', { style: { display: 'flex', gap: '12px', marginTop: '16px' } }, h('span', { class: 'tag bad' }, 'irreversible_final · 0.98'), h('span', { class: 'tag' }, 'not attempted')));
      const meter = h('div', { class: 'meter' }, h('u'), h('b', null, 'ordinary only ≥ 0.85'), h('i'));
      const meterCap = h('div', { style: { font: '400 19px/1.4 var(--mono)', color: '#9ca3af', marginTop: '16px' } }, 'below the bar, or any judge failure → treated as final');
      judge.append(meter, meterCap);
      stage2.append(vocabCap, vocab, noAuth, judge);
      root.append(stage2);
      // f03/f04: approval card + mode
      const acard = h('div', { class: 'acard abs', style: { left: '1100px', top: '210px', width: '720px', boxSizing: 'border-box' } },
        h('div', { style: { display: 'flex', alignItems: 'center', gap: '10px', font: '600 18px/1 var(--body)', color: '#b9a9ff' } }, icon('hand', 20, '#b9a9ff'), 'Lumen needs your input'),
        h('div', { style: { font: '500 25px/1.42 var(--body)', color: '#f4f4f6', marginTop: '16px' } }, 'Lumen reached the final action and paused for your approval. Should I execute “Submit application” now?'),
        h('div', { style: { display: 'flex', gap: '12px', marginTop: '22px' } }, h('span', { class: 'cbtn primary' }, h('span', { class: 'n' }, '1'), 'Submit'), h('span', { class: 'cbtn' }, h('span', { class: 'n' }, '2'), 'Leave it for my review')),
        h('div', { style: { font: '400 17px/1.45 var(--body)', color: '#a1a1aa', marginTop: '18px' } }, 'This choice is bound to the exact browser action shown above and expires if that target changes.'));
      const submitBtn = acard.querySelector('.cbtn.primary');
      const safeChip = h('div', { class: 'abs', style: { left: '1100px', top: '250px', display: 'flex', flexDirection: 'column', gap: '18px' } },
        h('div', { style: { display: 'inline-flex', alignItems: 'center', gap: '14px', padding: '16px 24px', borderRadius: '16px', background: '#121216', border: '1px solid #2b2b31', font: '600 30px/1 var(--body)', color: '#e4e4e7', alignSelf: 'flex-start' } }, icon('shield', 30, '#a1a1aa'), 'Safe approvals'),
        h('div', { style: { font: '500 20px/1.4 var(--mono)', color: '#8b909a' } }, 'the default for every new task'));
      root.append(safeChip);
      const modeMenu = h('div', { class: 'abs', style: { left: '1100px', top: '210px', width: '720px', borderRadius: '16px', background: '#121216', border: '1px solid #26262b', padding: '20px 22px', boxSizing: 'border-box', boxShadow: '0 30px 90px rgba(0,0,0,.6)' } },
        h('div', { style: { font: '600 20px/1.2 var(--body)', color: '#e4e4e7' } }, 'How should Lumen handle final actions?'),
        h('div', { class: 'modeopt', style: { display: 'flex', gap: '14px', marginTop: '16px', padding: '14px', borderRadius: '12px' } }, icon('shield', 24, '#a1a1aa'),
          h('div', null, h('div', { style: { font: '600 21px/1.2 var(--body)', color: '#f4f4f6' } }, 'Safe approvals'),
            h('div', { style: { font: '400 18px/1.4 var(--body)', color: '#a1a1aa', marginTop: '4px' } }, 'Work independently, but wait for you before consequential final actions.'))),
        h('div', { class: 'modeopt', style: { display: 'flex', gap: '14px', marginTop: '6px', padding: '14px', borderRadius: '12px' } }, icon('zap', 24, '#fbbf24'),
          h('div', null, h('div', { style: { font: '600 21px/1.2 var(--body)', color: '#fbbf24' } }, 'Full autonomy'),
            h('div', { style: { font: '400 18px/1.4 var(--body)', color: '#a1a1aa', marginTop: '4px' } }, 'Complete requested final actions without asking, including submits, sends, bookings, and purchases.'))),
        h('div', { style: { font: '400 16px/1.45 var(--body)', color: '#8b909a', marginTop: '12px', paddingTop: '12px', borderTop: '1px solid #1f1f24' } }, 'Both modes still require grounded facts, exact target binding, and verified outcomes.'));
      const opts = modeMenu.querySelectorAll('.modeopt');
      const done = h('div', { class: 'abs', style: { left: '1100px', top: '230px', width: '720px', display: 'flex', gap: '14px', alignItems: 'flex-start' } },
        h('div', { class: 'spark' }, icon('sparkles', 18, '#a996ff')),
        h('div', null, h('div', { style: { font: '400 22px/1.45 var(--body)', color: '#e4e4e7' } }, 'Lumen submitted the application and verified explicit completion evidence in the browser.'),
          h('div', { style: { display: 'flex', gap: '10px', marginTop: '14px' } }, h('span', { class: 'tag ok' }, 'COMPLETED'), h('span', { class: 'tag' }, '1 trusted activation'))));
      const sysPtr = h('div', { class: 'sysptr' });
      sysPtr.append(s('svg', { viewBox: '0 0 24 24', width: 40, height: 40 }, s('path', { d: 'M5 2.5 L5 18.5 L9.2 14.9 L11.8 20.8 L14.6 19.5 L12 13.8 L17.5 13.4 Z', fill: '#ffffff', stroke: '#111', 'stroke-width': 1.3, 'stroke-linejoin': 'round' })));
      const bind = s('svg', { width: 1920, height: 1080, style: 'position:absolute;left:0;top:0;pointer-events:none' });
      const bindPath = s('path', { d: 'M 1100 330 C 1000 330, 960 700, 640 0', fill: 'none', stroke: '#a996ff', 'stroke-width': 2.5, 'stroke-dasharray': '7 7' });
      bind.append(bindPath);
      const lockBadge = h('div', { class: 'abs tag vio', style: { left: '0', top: '0' } }, icon('lock', 18, '#b9a9ff'), 'bound: selector · text · role · page');
      root.append(bind, acard, modeMenu, done, lockBadge, sysPtr);
      // f05: what you see / what the model sees
      const mkSign = (masked) => {
        const box = h('div', { class: 'split' });
        const sign = h('div', { class: 'sign' }, h('div', { class: 't' }, 'Sign in to Acme Careers'));
        const fld = (label, val, m) => { const inp = h('div', { class: 'in' }, val); if (m) inp.append(h('div', { class: 'mask' })); sign.append(h('div', { class: 'f' }, h('div', { class: 'l' }, label), inp)); };
        fld('Email', 'jordan.avery@example.com', false);
        fld('Password', '••••••••••••', masked);
        fld('One-time code', '482 913', masked);
        sign.append(h('div', { class: 'btn' }, 'Continue'));
        box.append(sign);
        return box;
      };
      const youSee = mkSign(false); youSee.style.left = '150px';
      const modelSees = mkSign(true); modelSees.style.left = '990px';
      const cap1 = h('div', { class: 'abs kick', style: { left: '150px', width: '780px', textAlign: 'center', top: '160px' } }, 'What you see');
      const cap2 = h('div', { class: 'abs kick', style: { left: '990px', width: '780px', textAlign: 'center', top: '160px', color: '#5ee6bd' } }, 'What the model sees');
      const maskNote = h('div', { class: 'abs', style: { left: '0', right: '0', top: '868px', textAlign: 'center', font: '500 21px/1.5 var(--mono)', color: '#9ca3af' } },
        'masked in every frame before capture · if masking fails, the screenshot is discarded', h('br'),
        h('span', { style: { color: '#8ec8ff' } }, 'saved logins are typed by the body (use_credential); the model never receives them'));
      root.append(youSee, modelSees, cap1, cap2, maskNote);
      // f06: desktop
      const D = ui.desktop({ x: 110, y: 170, w: 1060, hh: 800 });
      const np = ui.nwin({ x: 120, y: 120, w: 760, hh: 470, title: 'declaration.txt · Notepad', menu: ['File', 'Edit', 'View'] });
      np.body.append(h('div', { style: { padding: '18px 22px', font: '400 21px/1.6 Consolas, var(--mono)', color: '#1b2430', whiteSpace: 'pre' } }, 'CUSTOMS DECLARATION\nContainer ID: ________________\nInspected by: Field Unit CF-3'));
      D.layer.append(np.el);
      const dframe = h('div', { class: 'frame' });
      D.el.append(dframe);
      const dpw = h('div', { class: 'pillwrap', style: { top: '16px' } });
      D.el.append(dpw);
      const dpill = F.Pill(dpw);
      const blocked = h('div', { class: 'blocked', style: { left: '1230px', top: '170px', width: '600px' } },
        h('div', { style: { font: '500 17px/1 var(--mono)', letterSpacing: '.14em', color: '#8b909a', marginBottom: '10px' } }, 'NEVER LAUNCHED'),
        ...['Command Prompt', 'PowerShell', 'Windows Terminal', 'WSL', 'Run dialog', 'Registry Editor'].map((n) => h('div', { class: 'row' }, icon('terminal', 22, '#ff8a95'), h('s', null, n))),
        h('div', { style: { font: '400 18px/1.4 var(--mono)', color: '#9ca3af', marginTop: '12px' } }, 'no shell primitive exists in the body'));
      const host = h('div', { class: 'hostbar', style: { left: '1230px', top: '650px', width: '600px' } },
        h('div', { style: { display: 'flex', alignItems: 'center', gap: '10px', font: '600 19px/1 var(--body)', color: '#f4f4f6', marginBottom: '8px' } }, 'Lumen Desktop', h('i', { class: 'hdot', style: { width: '9px', height: '9px', borderRadius: '50%', background: '#34d399', boxShadow: '0 0 10px #34d399', display: 'block' } })),
        h('div', { class: 'r' }, 'Runtime', h('span', { style: { color: '#34d399' } }, 'ready')),
        h('div', { class: 'r' }, 'Chrome body', h('span', { style: { color: '#34d399' } }, 'ready')));
      const cuVal = h('span', { style: { color: '#34d399' } }, 'on');
      const cuRow = h('div', { class: 'r' }, 'Computer use', cuVal);
      const pbtnLabel = h('span', null, 'Pause computer');
      const pbtn = h('div', { class: 'pbtn' }, icon('pause', 18, '#f4f4f6'), pbtnLabel);
      host.append(cuRow, pbtn);
      const hostPtr = h('div', { class: 'sysptr' });
      hostPtr.append(s('svg', { viewBox: '0 0 24 24', width: 40, height: 40 }, s('path', { d: 'M5 2.5 L5 18.5 L9.2 14.9 L11.8 20.8 L14.6 19.5 L12 13.8 L17.5 13.4 Z', fill: '#ffffff', stroke: '#111', 'stroke-width': 1.3, 'stroke-linejoin': 'round' })));
      root.append(D.el, blocked, host, hostPtr);
      this.r = { safeChip, t1, t2, bar, stage2, ch, jp, thanks, pointer, vocab, vocabCap, noAuth, judge, meter, meterNeedle: meter.querySelector('i'), acard, submitBtn, modeMenu, opts, done, sysPtr, bind, bindPath, lockBadge,
        youSee, modelSees, cap1, cap2, maskNote, D, dframe, dpill, blocked, host, cuVal, pbtn, pbtnLabel, hostPtr, laid: false };
    },
    _layout() {
      const r = this.r;
      r.sub = pos(r.jp.submit, r.ch.page);
      r.laid = true;
    },
    update(t, sc) {
      const r = this.r;
      if (!r.laid) this._layout();
      const tSome = LN('f01').start, tFloors = WD('f01', 'floors');
      const tModel = WD('f02', 'model'), tNever = WD('f02', 'never'), tBefore = WD('f02', 'Before'), tJudge = WD('f02', 'judge'), tConseq = WD('f02', 'consequential'), tConf = WD('f02', 'confident'), tTreated = WD('f02', 'treated');
      const tSafe = WD('f03', 'Safe'), tStops = WD('f03', 'stops'), tBound = WD('f03', 'bound'), tButton = WD('f03', 'button');
      const tFull = WD('f04', 'Full'), tEven = WD('f04', 'Even'), tFires = WD('f04', 'fires'), tOnce = WD('f04', 'once'), tVerifies = WD('f04', 'verifies');
      const tPw = WD('f05', 'Password'), tBlack = WD('f05', 'blacked'), tSees = WD('f05', 'sees');
      const tDesk = WD('f06', 'desktop'), tShells = WD('f06', 'shells'), tBlue = WD('f06', 'blue'), tSwitch = WD('f06', 'switch'), tPause = WD('f06', 'pause');
      const end = sc.end;
      // f01
      const k1 = P(t, tSome - 0.1, 0.7, E.outQ5), k2 = P(t, tFloors - 0.25, 0.5, E.outQ5);
      const o1 = 1 - P(t, tModel - 0.55, 0.45, E.inQ);
      set(r.t1, { o: k1 * o1, y: (1 - k1) * 20, blur: (1 - k1) * 8 });
      set(r.t2, { o: k2 * o1, s: 1.12 - 0.12 * k2, blur: (1 - k2) * 10 });
      css(r.bar, 'transform', `scaleX(${P(t, WE('f01', 'floors') - 0.02, 0.3, E.outE).toFixed(3)})`);
      set(r.bar, { o: o1 });
      // f02 + f03 + f04 share the page
      const kS2 = P(t, tModel - 0.02, 0.6, E.outQ5);
      const s2out = 1 - P(t, tPw - 0.5, 0.45, E.inQ);
      set(r.stage2, { o: kS2 * s2out });
      const kv = P(t, tModel, 0.6, E.outQ5);
      const vOut = 1 - P(t, tSafe - 0.6, 0.4, E.inQ);
      set(r.vocabCap, { o: kv * vOut });
      Array.from(r.vocab.children).forEach((el, i) => { const k = P(t, tModel + i * 0.025, 0.4, E.outQ5); set(el, { o: k * vOut, y: (1 - k) * 8 }); });
      set(r.noAuth, { o: P(t, tNever + 0.1, 0.5) * vOut });
      const kj = P(t, tBefore, 0.6, E.outQ5);
      set(r.judge, { o: kj * vOut, y: (1 - kj) * 24 });
      const needle = t < tConf ? lerp(0.3, 0.98, P(t, tJudge, 1.0, E.outC)) : lerp(0.98, 0.62, P(t, tConf, 0.8, E.ioC));
      css(r.meterNeedle, 'left', (needle * 100).toFixed(2) + '%');
      // pointer to the submit button
      const sb = r.sub;
      const ptrTarget = { x: sb.x + sb.w * 0.55, y: sb.y + sb.h * 0.6 - 380 };
      const pp = F.track(t, [[tBefore - 0.2, 700, 760], [tBefore + 0.8, ptrTarget.x, ptrTarget.y]], E.ioC);
      const fireAt = tFires - 0.05;
      r.pointer.update(t, pp.x, pp.y, env(t, tBefore - 0.3, tVerifies + 1, 0.3, 0.4), t < tSafe ? tBefore + 0.85 : fireAt);
      set(r.thanks, { o: P(t, tOnce, 0.5, E.outC) });
      // approval card (preceded by the mode chip)
      set(r.safeChip, { o: env(t, tSafe - 0.2, tStops - 0.2, 0.4, 0.3), y: (1 - P(t, tSafe - 0.2, 0.5, E.outQ5)) * 16 });
      const ka = env(t, tStops - 0.35, tFull + 0.1, 0.5, 0.35);
      set(r.acard, { o: ka, y: (1 - P(t, tStops - 0.35, 0.5, E.outQ5)) * 20 });
      const pressed = t > tButton + 0.25 && t < tButton + 0.45;
      set(r.submitBtn, { s: pressed ? 0.95 : 1 });
      // binding line from the card to the exact button
      const kb = env(t, tBound - 0.1, tFull + 0.1, 0.6, 0.3);
      const bx = 60 + sb.x + sb.w, by = 120 + 50 + sb.y + sb.h / 2 - 380;
      r.bindPath.setAttribute('d', `M 1100 340 C 1060 340, ${bx + 120} ${by}, ${bx + 6} ${by}`);
      F.drawPath(r.bindPath, P(t, tBound - 0.1, 0.6));
      set(r.bind, { o: kb });
      set(r.lockBadge, { o: kb, x: bx + 30, y: by + 24 });
      // the user's cursor clicks Submit
      const spos = F.track(t, [[tStops, 1500, 980], [tButton + 0.2, 1192, 392]], E.ioC);
      set(r.sysPtr, { o: env(t, tStops, tFull, 0.3, 0.3), x: spos.x, y: spos.y });
      // mode menu (f04)
      const km = env(t, tFull - 0.05, tOnce + 0.35, 0.4, 0.3);
      set(r.modeMenu, { o: km, y: (1 - P(t, tFull - 0.05, 0.5, E.outQ5)) * 16 });
      css(r.opts[1], 'background', t > WD('f04', 'autonomy') ? 'rgba(251,191,36,.08)' : 'transparent');
      css(r.opts[1], 'box-shadow', t > WD('f04', 'autonomy') ? 'inset 0 0 0 1.5px rgba(251,191,36,.45)' : 'none');
      const kd = P(t, tOnce + 0.3, 0.5, E.outQ5);
      set(r.done, { o: kd * (1 - P(t, tPw - 0.5, 0.4)), y: (1 - kd) * 14 });
      // f05
      const k5 = P(t, tPw - 0.3, 0.7, E.outQ5);
      const o5 = 1 - P(t, tDesk - 0.45, 0.4, E.inQ);
      set(r.youSee, { o: k5 * o5, x: (1 - k5) * -40 });
      set(r.modelSees, { o: k5 * o5, x: (1 - k5) * 40 });
      set(r.cap1, { o: k5 * o5 }); set(r.cap2, { o: k5 * o5 });
      for (const m of r.modelSees.querySelectorAll('.mask')) set(m, { o: P(t, tBlack - 0.1, 0.3) });
      set(r.maskNote, { o: P(t, tSees, 0.5) * o5 });
      // f06
      const k6 = P(t, tDesk - 0.3, 0.7, E.outQ5);
      const paused = t >= tPause + 0.1;
      const o6 = 1 - P(t, end - 0.4, 0.45, E.inQ);
      set(r.D.el, { o: k6 * o6, y: (1 - k6) * 30 });
      set(r.dframe, { o: (paused ? 0 : 1) * P(t, tBlue - 0.1, 0.4) * (1 + 0.15 * Math.sin(t * 6) * env(t, tBlue, tSwitch, 0.2, 0.2)) });
      r.dpill.update(t, (paused ? 0 : 1) * P(t, tDesk, 0.4), [[tDesk, 'Lumen is using your computer']]);
      const kbk = P(t, tShells - 0.2, 0.5, E.outQ5);
      set(r.blocked, { o: kbk * o6, x: (1 - kbk) * 30 });
      const kh = P(t, tSwitch - 0.6, 0.5, E.outQ5);
      set(r.host, { o: kh * o6, y: (1 - kh) * 20 });
      text(r.cuVal, paused ? 'paused' : 'on');
      text(r.pbtnLabel, paused ? 'Enable computer' : 'Pause computer');
      css(r.cuVal, 'color', paused ? '#fbbf24' : '#34d399');
      set(r.pbtn, { s: t > tPause - 0.05 && t < tPause + 0.12 ? 0.95 : 1 });
      const hp = F.track(t, [[tSwitch - 0.5, 1800, 1040], [tPause - 0.05, 1330, 870]], E.ioC);
      set(r.hostPtr, { o: env(t, tSwitch - 0.5, end, 0.3, 0.4), x: hp.x, y: hp.y });
    },
  });
})();
