/* Lumen launch film: scenes 3-4 (brain and body, the loop). */
(function () {
  'use strict';
  const F = window.FILM;
  const { E, P, env, clamp, lerp, h, s, set, css, text, cls, WD, WE, LN, icon, pos } = F;
  const ui = F.ui;

  // =====================================================================================
  // Scene 3: brain and body
  // =====================================================================================
  const BRAINS = ['GLM 5.3 Flash', 'GPT-6 Luna', 'MiMo V2.5', 'Gemini 2.5 Flash', 'MiniMax M3', 'Qwen 3.5 9B', 'Claude Opus 5.5', 'GLM 5.3 Flash'];
  F.scene('brain', {
    build(root) {
      root.append(h('style', null, `
        .node{position:absolute;border-radius:26px;padding:26px 30px;box-sizing:border-box;background:linear-gradient(180deg,rgba(255,255,255,.06),rgba(255,255,255,.02));
          border:1.5px solid rgba(255,255,255,.12);box-shadow:0 30px 90px rgba(0,0,0,.55),inset 0 1px 0 rgba(255,255,255,.06);}
        .node .k{font:500 17px/1 var(--mono);letter-spacing:.16em;text-transform:uppercase;color:var(--mute);display:flex;align-items:center;gap:12px}
        .node .t{font:600 46px/1.05 var(--display);letter-spacing:-.03em;color:#fff;margin-top:14px}
        .node .d{font:400 22px/1.4 var(--body);color:var(--ink2);margin-top:10px}
        .node.lit{border-color:rgba(85,173,255,.7);box-shadow:0 0 0 1px rgba(85,173,255,.35),0 0 60px rgba(85,173,255,.22),0 30px 90px rgba(0,0,0,.55);}
        .node.mintlit{border-color:rgba(94,230,189,.7);box-shadow:0 0 0 1px rgba(94,230,189,.35),0 0 60px rgba(94,230,189,.2),0 30px 90px rgba(0,0,0,.55);}
        .seat{position:absolute;right:26px;top:24px;display:flex;align-items:center;gap:10px;padding:8px 14px;border-radius:999px;background:rgba(85,173,255,.1);border:1px solid rgba(85,173,255,.4);font:600 19px/1 var(--mono);color:var(--blue2)}
        .seg{flex:1;display:flex;align-items:center;justify-content:center;gap:12px;border-radius:14px;font:600 26px/1 var(--display);letter-spacing:-.01em;color:#cfd3da;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.08)}
        .seg.on{color:#e9fff7;background:rgba(94,230,189,.14);border-color:rgba(94,230,189,.85);box-shadow:0 0 0 1px rgba(94,230,189,.25),0 0 44px rgba(94,230,189,.22)}
        .lbl{position:absolute;font:500 19px/1.35 var(--mono);color:var(--ink2);letter-spacing:.01em}
        .strike{position:absolute;left:0;right:0;text-align:center;font:500 36px/1.2 var(--mono);color:#d4d7de;white-space:nowrap;font-variant-ligatures:none}
        .strike .code{position:relative;display:inline-block}
        .strike i{position:absolute;left:-12px;right:-12px;top:52%;height:4px;border-radius:2px;background:var(--red);transform-origin:0 50%;box-shadow:0 0 14px rgba(255,108,120,.6)}
      `));
      const dia = h('div', { class: 'abs', style: { inset: '0' } });
      root.append(dia);
      // "two halves": a split title card, then the diagram builds in its place
      const divider = h('div', { class: 'abs', style: { left: '360px', right: '360px', top: '540px', height: '2px', background: 'linear-gradient(90deg,rgba(94,230,189,0),rgba(94,230,189,.8),rgba(94,230,189,0))', transformOrigin: '50% 50%' } });
      const kBrain = h('div', { class: 'abs', style: { left: '0', right: '0', top: '330px', textAlign: 'center', font: '600 150px/1 var(--display)', letterSpacing: '-.05em', color: '#f4f4f6' } }, 'Brain');
      const kBody = h('div', { class: 'abs', style: { left: '0', right: '0', top: '600px', textAlign: 'center', font: '600 150px/1 var(--display)', letterSpacing: '-.05em', color: '#8ec8ff' } }, 'Body');
      root.append(divider, kBrain, kBody);
      // svg layer for flows
      const svg = s('svg', { width: 1920, height: 1080, viewBox: '0 0 1920 1080', style: 'position:absolute;left:0;top:0;overflow:visible' });
      const defs = s('defs', null,
        s('marker', { id: 'arrB', viewBox: '0 0 10 10', refX: 8, refY: 5, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse' }, s('path', { d: 'M0 0L10 5L0 10z', fill: '#55adff' })),
        s('marker', { id: 'arrM', viewBox: '0 0 10 10', refX: 8, refY: 5, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse' }, s('path', { d: 'M0 0L10 5L0 10z', fill: '#5ee6bd' })));
      svg.append(defs);
      const mk = (d, color, marker, dash) => s('path', { d, fill: 'none', stroke: color, 'stroke-width': 3, 'stroke-linecap': 'round', 'marker-end': marker ? `url(#${marker})` : null, 'stroke-dasharray': dash || null });
      const upL = mk('M 560 760 L 560 640', '#55adff', 'arrB');
      const upL2 = mk('M 880 500 L 880 384', '#55adff', 'arrB');
      const dnR = mk('M 1040 384 L 1040 498', '#5ee6bd', 'arrM');
      const dnR2 = mk('M 1360 642 L 1360 758', '#5ee6bd', 'arrM');
      const upR = mk('M 1300 760 L 1300 640', '#55adff', 'arrB');
      const dnL = mk('M 620 642 L 620 758', '#5ee6bd', 'arrM');
      const sw = mk('M 860 900 C 960 990, 960 990, 1060 900', '#8ec8ff', 'arrB');
      const swBack = mk('M 1060 880 C 960 800, 960 800, 860 880', '#8ec8ff', 'arrB');
      svg.append(upL, upL2, dnR, dnR2, upR, dnL, sw, swBack);
      dia.append(svg);
      // brain node
      const seatName = h('span');
      const seat = h('div', { class: 'seat' }, h('i', { class: 'dot', style: { color: '#55adff', width: '9px', height: '9px' } }), seatName);
      const brain = h('div', { class: 'node', style: { left: '640px', top: '150px', width: '640px', height: '228px' } },
        h('div', { class: 'k' }, icon('eye', 20, '#8b909a'), 'Brain'),
        h('div', { class: 't' }, 'A vision model'),
        h('div', { class: 'd' }, 'Sees the screen. Chooses one next action.'), seat);
      dia.append(brain);
      // input thumbnail + output action chip
      const thumb = h('div', { class: 'abs', style: { left: '238px', top: '176px', width: '300px', height: '180px', borderRadius: '12px', overflow: 'hidden', background: '#fff', boxShadow: '0 20px 60px rgba(0,0,0,.5), 0 0 0 1px rgba(255,255,255,.12)' } });
      const thumbInner = h('div', { style: { transform: 'scale(.3)', transformOrigin: '0 0', width: '1000px', height: '600px', position: 'relative' } });
      thumb.append(thumbInner);
      ui.jobPage(thumbInner);
      const thumbLbl = h('div', { class: 'lbl', style: { left: '238px', top: '364px' } }, 'screenshot · page text · controls');
      const act = h('div', { class: 'abs', style: { left: '1360px', top: '206px', padding: '18px 22px', borderRadius: '14px', background: '#0b0d11', border: '1.5px solid rgba(94,230,189,.45)', font: '500 22px/1.5 var(--mono)', color: '#c7cad1', boxShadow: '0 0 40px rgba(94,230,189,.12)' } });
      act.innerHTML = '<span class="jp">{</span> <span class="jk">"action"</span><span class="jp">:</span> <span class="js">"type"</span><span class="jp">,</span><br>&nbsp;&nbsp;<span class="jk">"control"</span><span class="jp">:</span> <span class="jn">6</span><span class="jp">, …</span> <span class="jp">}</span>';
      const actLbl = h('div', { class: 'lbl', style: { left: '1360px', top: '330px' } }, 'one action per turn');
      const flowIn = s('path', { d: 'M 540 266 L 636 266', fill: 'none', stroke: '#55adff', 'stroke-width': 3, 'marker-end': 'url(#arrB)' });
      const flowOut = s('path', { d: 'M 1284 266 L 1356 266', fill: 'none', stroke: '#5ee6bd', 'stroke-width': 3, 'marker-end': 'url(#arrM)' });
      svg.append(flowIn, flowOut);
      dia.append(thumb, thumbLbl, act, actLbl);
      const brainsCap = h('div', { class: 'abs mono', style: { left: '385px', top: '396px', width: '1150px', textAlign: 'center', font: '500 18px/1.6 var(--mono)', color: '#8b909a' } },
        '17 different brains have driven Lumen in recorded runs, among them', h('br'), h('span', { style: { color: '#c7cad1' } }, 'GLM 5.3 Flash · GPT-6 Luna · MiMo V2.5 · Gemini 2.5 Flash · MiniMax M3 · Qwen 3.5 9B · Claude Opus 5.5'));
      dia.append(brainsCap);
      // runtime
      const segs = ['binds', 'executes', 'verifies'].map((w) => h('div', { class: 'seg' }, w));
      const runtime = h('div', { class: 'node', style: { left: '330px', top: '500px', width: '1260px', height: '142px', padding: '20px 24px', display: 'flex', gap: '18px', alignItems: 'stretch' } },
        h('div', { style: { width: '300px', display: 'flex', flexDirection: 'column', justifyContent: 'center', gap: '10px' } },
          h('div', { class: 'k', style: { font: '500 17px/1 var(--mono)', letterSpacing: '.16em', textTransform: 'uppercase', color: '#8b909a', display: 'flex', gap: '10px', alignItems: 'center' } }, icon('cpu', 20, '#8b909a'), 'Runtime'),
          h('div', { style: { font: '600 30px/1.1 var(--display)', letterSpacing: '-.02em', color: '#fff' } }, 'Deterministic code')),
        ...segs);
      const motto = h('div', { class: 'abs', style: { left: '330px', width: '1260px', top: '652px', textAlign: 'center', font: '500 21px/1 var(--mono)', letterSpacing: '.06em', color: '#5ee6bd' } }, 'intelligence proposes · determinism enforces');
      dia.append(runtime, motto);
      // bodies
      const chromeB = h('div', { class: 'node', style: { left: '250px', top: '760px', width: '600px', height: '214px' } },
        h('div', { class: 'k' }, icon('browser', 20, '#8b909a'), 'Body'),
        h('div', { class: 't', style: { fontSize: '40px' } }, 'Chrome extension'),
        h('div', { class: 'd' }, 'Inside your real Chrome, with your sessions.'));
      const winB = h('div', { class: 'node', style: { left: '1070px', top: '760px', width: '600px', height: '214px' } },
        h('div', { class: 'k' }, icon('monitor', 20, '#8b909a'), 'Body'),
        h('div', { class: 't', style: { fontSize: '40px' } }, 'Windows desktop'),
        h('div', { class: 'd' }, 'UI Automation, real mouse and keyboard.'));
      const swLbl = h('div', { class: 'lbl', style: { left: '0', width: '1920px', textAlign: 'center', top: '1000px', color: '#8ec8ff' } }, 'switch_surface');
      const vmNote = h('div', { class: 'lbl', style: { left: '1070px', top: '984px', color: '#8b909a', fontSize: '17px' } }, '+ an Ubuntu VM body, for OSWorld evaluation');
      const token = h('div', { class: 'abs', style: { left: '0', top: '0', width: '18px', height: '18px', marginLeft: '-9px', marginTop: '-9px', borderRadius: '50%', background: '#8ec8ff', boxShadow: '0 0 18px #55adff, 0 0 40px #55adff' } });
      dia.append(chromeB, winB, swLbl, vmNote, token);
      // flow labels
      const lEnum = h('div', { class: 'lbl', style: { left: '600px', top: '412px', color: '#8ec8ff', textAlign: 'right', width: '264px' } }, 'enumerated evidence');
      const lAct = h('div', { class: 'lbl', style: { left: '1060px', top: '412px', color: '#5ee6bd' } }, 'one proposed action');
      dia.append(lEnum, lAct);
      // b05: struck scripts + loop ring
      const strikes = [
        'if (site == "greenhouse") { … }',
        'if (label == "Phone") type(profile.phone)',
        'click("#submit-application")',
      ].map((code, i) => { const line = h('i'); const el = h('div', { class: 'strike', style: { top: (380 + i * 92) + 'px' } }, h('span', { class: 'code' }, code, line)); el.__line = line; root.append(el); return el; });
      const ringSvg = s('svg', { width: 1920, height: 1080, viewBox: '0 0 1920 1080', style: 'position:absolute;left:0;top:0' });
      const ring = s('circle', { cx: 960, cy: 540, r: 190, fill: 'none', stroke: '#5ee6bd', 'stroke-width': 3, transform: 'rotate(-90 960 540)' });
      const ringGlow = s('circle', { cx: 960, cy: 540, r: 190, fill: 'none', stroke: '#5ee6bd', 'stroke-width': 14, opacity: 0.12, transform: 'rotate(-90 960 540)' });
      const ringDot = s('circle', { cx: 960, cy: 350, r: 9, fill: '#d9fff2' });
      ringSvg.append(ringGlow, ring, ringDot);
      const ringTxt = h('div', { class: 'abs', style: { left: '0', width: '1920px', top: '510px', textAlign: 'center', font: '600 52px/1 var(--display)', letterSpacing: '-.03em' } }, 'One general loop');
      root.append(ringSvg, ringTxt);
      this.r = { dia, divider, kBrain, kBody, svg, upL, upL2, dnR, dnR2, upR, dnL, sw, swBack, brain, seat, seatName, thumb, thumbLbl, act, actLbl, flowIn, flowOut, brainsCap,
        runtime, segs, motto, chromeB, winB, swLbl, vmNote, token, lEnum, lAct, strikes, ringSvg, ring, ringGlow, ringDot, ringTxt };
    },
    update(t, sc) {
      const r = this.r;
      const t2 = WD('b01', 'two'), tHalves = WD('b01', 'halves');
      const tBrain = WD('b02', 'brain'), tLooks = WD('b02', 'looks'), tChooses = WD('b02', 'chooses'), tAny = WD('b02', 'Any'), tSeat = WE('b02', 'seat');
      const tBody = WD('b03', 'body'), tChrome = WD('b03', 'Chrome'), tWin = WD('b03', 'Windows'), tOne = WD('b03', 'One'), tEndB3 = LN('b03').end;
      const tEnum = WD('b04', 'enumerates'), tInterp = WD('b04', 'interprets'), tDet = WD('b04', 'Deterministic'), tBinds = WD('b04', 'binds'), tExec = WD('b04', 'executes'), tVer = WD('b04', 'verifies');
      const tNo = WD('b05', 'No'), tGen = WD('b05', 'One');
      const fin = 1 - P(t, tNo - 0.2, 0.7, E.inQ);
      // halves: the split title card
      const kd = env(t, t2 - 0.5, tBrain - 0.05, 0.6, 0.45);
      css(r.divider, 'transform', `scaleX(${E.outQ5(clamp((t - (t2 - 0.5)) / 0.9)).toFixed(3)})`);
      set(r.divider, { o: kd });
      const kb1 = P(t, t2 - 0.15, 0.7, E.outQ5), kb2 = P(t, tHalves - 0.05, 0.7, E.outQ5);
      const away = P(t, tBrain - 0.45, 0.45, E.inQ);
      set(r.kBrain, { o: kb1 * (1 - away), y: (1 - kb1) * 40 - away * 60, blur: (1 - kb1) * 12 + away * 10 });
      set(r.kBody, { o: kb2 * (1 - away), y: (1 - kb2) * -40 + away * 60, blur: (1 - kb2) * 12 + away * 10 });
      // camera: keep the brain centered until the bodies arrive, then settle on the full diagram
      const cam = 250 * (1 - P(t, tBody - 0.5, 1.3, E.ioE));
      set(r.dia, { y: cam });
      // brain
      const kB = P(t, tBrain - 0.2, 0.8, E.outQ5);
      set(r.brain, { o: kB * fin, y: (1 - kB) * 30, s: 0.96 + 0.04 * kB });
      cls(r.brain, 'lit', (t > tLooks && t < tSeat + 0.5) || (t > tInterp - 0.1 && t < tDet));
      const kT = P(t, tLooks - 0.3, 0.7, E.outQ5);
      set(r.thumb, { o: kT * fin * (1 - P(t, tBody, 0.6)), x: (1 - kT) * -50 });
      set(r.thumbLbl, { o: kT * fin * (1 - P(t, tBody, 0.6)) });
      F.drawPath(r.flowIn, P(t, tLooks, 0.5));
      set(r.flowIn, { o: (r.flowIn.__k > 0.01 ? 1 : 0) * fin * (1 - P(t, tBody, 0.6)) });
      const kA = P(t, tChooses + 0.1, 0.6, E.outQ5);
      set(r.act, { o: kA * fin * (1 - P(t, tBody, 0.6)), x: (1 - kA) * 40 });
      set(r.actLbl, { o: kA * fin * (1 - P(t, tBody, 0.6)) });
      F.drawPath(r.flowOut, P(t, tChooses, 0.4));
      set(r.flowOut, { o: (r.flowOut.__k > 0.01 ? 1 : 0) * fin * (1 - P(t, tBody, 0.6)) });
      // seat carousel
      const span = Math.max(0.3, (tSeat - tAny) / (BRAINS.length - 1));
      let idx = 0;
      if (t >= tAny) idx = Math.min(BRAINS.length - 1, Math.floor((t - tAny) / span));
      text(r.seatName, BRAINS[idx] + (idx === BRAINS.length - 1 || t < tAny ? ' · today' : ''));
      const flick = t >= tAny && t < tSeat ? 0.75 + 0.25 * Math.cos(((t - tAny) / span) * Math.PI * 2) : 1;
      set(r.seat, { o: P(t, tBrain + 0.3, 0.5) * flick });
      set(r.brainsCap, { o: env(t, tAny + 0.2, tChrome - 0.2, 0.6, 0.5) * fin });
      // bodies
      const kPre = 0.4 * P(t, tBody + 0.25, 0.8, E.outQ5);
      const kC = P(t, tChrome - 0.25, 0.7, E.outQ5), kW = P(t, tWin - 0.25, 0.7, E.outQ5);
      set(r.chromeB, { o: Math.max(kPre, kC) * fin, y: (1 - Math.max(kPre * 2.5, kC)) * 30 });
      set(r.winB, { o: Math.max(kPre, kW) * fin, y: (1 - Math.max(kPre * 2.5, kW)) * 30 });
      cls(r.chromeB, 'lit', t > tChrome && t < tWin);
      cls(r.winB, 'lit', t > tWin && t < tOne);
      set(r.vmNote, { o: env(t, tWin + 0.8, tNo, 0.6, 0.4) * 0.9 });
      // switch arc + token
      const kS = env(t, tOne - 0.2, tEnum, 0.5, 0.6);
      set(r.sw, { o: kS }); set(r.swBack, { o: kS }); set(r.swLbl, { o: kS });
      F.drawPath(r.sw, P(t, tOne - 0.2, 0.6)); F.drawPath(r.swBack, P(t, tOne + 0.2, 0.6));
      if (t > tOne && t < tEnum) {
        const cyc = ((t - tOne) / 1.3) % 2;
        const pt = cyc < 1 ? F.along(r.sw, E.ioC(cyc)) : F.along(r.swBack, E.ioC(cyc - 1));
        set(r.token, { o: kS, x: pt.x, y: pt.y });
      } else set(r.token, { o: 0 });
      // b04: runtime + flows
      const kR = P(t, tEnum - 0.6, 0.8, E.outQ5);
      set(r.runtime, { o: kR * fin, y: (1 - kR) * 20 });
      const flowsUp = P(t, tEnum - 0.1, 0.7, E.ioC);
      for (const p of [r.upL, r.upR]) { F.drawPath(p, flowsUp); set(p, { o: fin }); }
      F.drawPath(r.upL2, P(t, tEnum + 0.4, 0.6, E.ioC)); set(r.upL2, { o: fin });
      F.drawPath(r.dnR, P(t, tInterp, 0.6, E.ioC)); set(r.dnR, { o: fin });
      for (const p of [r.dnR2, r.dnL]) { F.drawPath(p, P(t, tExec, 0.6, E.ioC)); set(p, { o: fin }); }
      set(r.lEnum, { o: P(t, tEnum + 0.3, 0.5) * fin });
      set(r.lAct, { o: P(t, tInterp + 0.2, 0.5) * fin });
      cls(r.segs[0], 'on', t >= tBinds && t < tNo);
      cls(r.segs[1], 'on', t >= tExec && t < tNo);
      cls(r.segs[2], 'on', t >= tVer && t < tNo);
      cls(r.runtime, 'mintlit', t >= tBinds && t < tNo);
      set(r.motto, { o: P(t, tVer + 0.4, 0.6) * fin });
      // dashes flowing on the vertical connectors
      const dash = (-(t * 60) % 40).toFixed(1);
      for (const p of [r.upL, r.upR, r.upL2, r.dnR, r.dnR2, r.dnL]) {
        if (t > tVer + 0.6) { p.__flow = true; css(p, 'stroke-dasharray', '10 10'); css(p, 'stroke-dashoffset', dash); } else if (p.__flow) { p.__flow = false; css(p, 'stroke-dasharray', String(p.__len)); }
      }
      // b05: scripts struck, then the ring
      r.strikes.forEach((el, i) => {
        const a = tNo + 0.05 + i * 0.32;
        const k = P(t, a, 0.45, E.outQ5);
        const out = P(t, tGen - 0.35, 0.4, E.inQ);
        set(el, { o: k * (1 - out), y: (1 - k) * 12 });
        css(el.__line, 'transform', `scaleX(${P(t, a + 0.35, 0.35, E.ioC).toFixed(3)})`);
      });
      const kRing = P(t, tGen - 0.15, 1.1, E.ioC);
      F.drawPath(r.ring, kRing); F.drawPath(r.ringGlow, kRing);
      const ang = -Math.PI / 2 + kRing * Math.PI * 2 + Math.max(0, t - (tGen + 0.95)) * 2.4;
      set(r.ringDot, { o: kRing > 0 ? 1 : 0 });
      r.ringDot.setAttribute('cx', (960 + 190 * Math.cos(ang)).toFixed(1));
      r.ringDot.setAttribute('cy', (540 + 190 * Math.sin(ang)).toFixed(1));
      const kTx = P(t, tGen + 0.1, 0.7, E.outQ5);
      set(r.ringTxt, { o: kTx * (1 - P(t, sc.end - 0.45, 0.35)), blur: (1 - kTx) * 10 });
      set(r.ringSvg, { o: 1 - P(t, sc.end - 0.3, 0.4) });
    },
  });

  // =====================================================================================
  // Scene 4: the loop, one step of a job application
  // =====================================================================================
  const INV = [
    '{ "index": 4, "kind": "input/email", "value": "jordan.avery@example.com", "context": "Email *" }',
    '{ "index": 5, "kind": "select/combobox", "value": "+1", "context": "Country code" }',
    '{ "index": 6, "kind": "input/tel", "value": "", "context": "Phone *", "box": [478, 573, 550, 46] }',
    '{ "index": 7, "kind": "button", "text": "Attach", "context": "Resume/CV *" }',
  ];
  const ACTION = [
    [0, null, '{', ''],
    [1, 'action', 'type', ','],
    [1, 'control', 6, ','],
    [1, 'text', '(555) 010-0147', ','],
    [1, 'question', 'Phone', ','],
    [1, 'grounding_source', 'profile', ','],
    [1, 'grounding_quote', 'phone: (555) 010-0147', ','],
    [1, 'reason', 'Phone is empty; the profile has it.', ','],
    [1, 'then', '[]', ''],
    [0, null, '}', ''],
  ];
  F.scene('loop', {
    build(root) {
      root.append(h('style', null, `
        .ring-lbl{position:absolute;transform:translate(-50%,-50%);font:600 19px/1 var(--mono);letter-spacing:.14em;color:#6b7280;padding:9px 14px;border-radius:999px;background:#0b0d11;border:1.5px solid rgba(255,255,255,.12)}
        .ring-lbl.on{color:#05110d;background:var(--mint);border-color:var(--mint);box-shadow:0 0 30px rgba(94,230,189,.45)}
        .ring-lbl.gate.on{background:var(--amber);border-color:var(--amber);box-shadow:0 0 30px rgba(246,170,40,.45)}
        .dcard{position:absolute;left:1150px;top:470px;width:710px;height:500px;border-radius:20px;padding:26px 28px;box-sizing:border-box;background:#0b0d11;border:1px solid rgba(255,255,255,.09);box-shadow:0 30px 90px rgba(0,0,0,.55);overflow:hidden}
        .dhead{font:500 17px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase;color:#8b909a;display:flex;justify-content:space-between}
        .lp-sub{position:absolute;left:28px;right:28px;top:70px;bottom:24px}
        .inv{font:400 17px/1.55 var(--mono);color:#9ca3af;white-space:pre-wrap}
        .inv div{padding:7px 10px;border-radius:8px;margin-bottom:8px;background:rgba(255,255,255,.02)}
        .inv .hot{background:rgba(85,173,255,.12);box-shadow:0 0 0 1.5px rgba(85,173,255,.6);color:#dbeafe}
        .chk{display:flex;align-items:flex-start;gap:16px;padding:16px 18px;border-radius:14px;background:rgba(255,255,255,.025);border:1px solid rgba(255,255,255,.07);margin-bottom:14px}
        .chk .ic{width:34px;height:34px;border-radius:50%;display:grid;place-items:center;background:rgba(255,255,255,.06);flex:none}
        .chk.ok .ic{background:rgba(94,230,189,.16)}
        .chk b{display:block;font:600 24px/1.2 var(--display);color:#fff}
        .chk span{display:block;font:400 18px/1.45 var(--mono);color:#9ca3af;margin-top:6px}
        .chk span em{font-style:normal;color:#a7f3d0;background:rgba(94,230,189,.1);padding:1px 6px;border-radius:5px}
        .big-ok{font:600 34px/1.2 var(--display);letter-spacing:-.02em;color:#fff}
        .stepctr{display:flex;align-items:baseline;gap:16px;font:600 96px/1 var(--display);letter-spacing:-.04em;color:#fff}
        .stepctr small{font:500 19px/1 var(--mono);letter-spacing:.12em;text-transform:uppercase;color:#8b909a}
      `));
      // browser with the job page
      const ch = ui.chrome({ x: 60, y: 120, w: 1050, hh: 920, tab: 'Careers · Acme Robotics', url: '<b>jobs.acme.example</b>/careers/4821/apply', avatar: 'JA' });
      const jp = ui.jobPage(ch.page);
      root.append(ch.el);
      const flash = h('div', { class: 'abs', style: { inset: '0', background: '#fff', pointerEvents: 'none' } });
      const scan = h('div', { class: 'abs', style: { left: '0', right: '0', height: '140px', background: 'linear-gradient(180deg,rgba(85,173,255,0),rgba(85,173,255,.28),rgba(85,173,255,0))', borderBottom: '2px solid rgba(85,173,255,.9)' } });
      ch.page.append(flash, scan);
      const pointer = F.Pointer(ch.page);
      // pill
      const pw = h('div', { class: 'pillwrap' });
      root.append(pw);
      const pill = F.Pill(pw);
      // ring
      const RX = 1505, RY = 262, RR = 150;
      const rsvg = s('svg', { width: 1920, height: 1080, viewBox: '0 0 1920 1080', style: 'position:absolute;left:0;top:0' });
      const base = s('circle', { cx: RX, cy: RY, r: RR, fill: 'none', stroke: 'rgba(255,255,255,.12)', 'stroke-width': 2 });
      const prog = s('circle', { cx: RX, cy: RY, r: RR, fill: 'none', stroke: '#5ee6bd', 'stroke-width': 3, transform: `rotate(-90 ${RX} ${RY})` });
      const gate = s('path', { d: `M ${RX + RR * Math.cos(0.25)} ${RY + RR * Math.sin(0.25)} A ${RR} ${RR} 0 0 1 ${RX + RR * Math.cos(1.3)} ${RY + RR * Math.sin(1.3)}`, fill: 'none', stroke: '#f6aa28', 'stroke-width': 7, 'stroke-linecap': 'round', opacity: 0.25 });
      const dot = s('circle', { cx: RX, cy: RY - RR, r: 10, fill: '#e9fff7' });
      rsvg.append(base, gate, prog, dot);
      root.append(rsvg);
      const L = (txt, x, y, extra = '') => { const el = h('div', { class: 'ring-lbl ' + extra, style: { left: x + 'px', top: y + 'px' } }, txt); root.append(el); return el; };
      const lab = {
        obs: L('OBSERVE', RX, RY - RR), dec: L('DECIDE', RX + RR + 6, RY - 40), chk: L('CHECK', RX + RR - 16, RY + 96, 'gate'),
        act: L('ACT', RX, RY + RR), ver: L('VERIFY', RX - RR, RY),
      };
      const center = h('div', { class: 'abs', style: { left: (RX - 110) + 'px', top: (RY - 26) + 'px', width: '220px', textAlign: 'center', font: '500 17px/1.5 var(--mono)', color: '#8b909a', letterSpacing: '.06em' } }, 'one step', h('br'), h('span', { style: { color: '#c7cad1' } }, 'of many'));
      root.append(center);
      // detail card with sub-panels
      const dTitle = h('span'), dRight = h('span');
      const card = h('div', { class: 'dcard' }, h('div', { class: 'dhead' }, dTitle, dRight));
      const subInv = h('div', { class: 'lp-sub inv' });
      const invRows = INV.map((line, i) => { const d = h('div', { class: i === 2 ? 'hot' : '' }); subInv.append(d); return [d, line]; });
      const subAct = h('div', { class: 'lp-sub' });
      const json = F.JsonBlock(subAct, ACTION, 'json');
      json.el.style.font = '400 22px/1.62 var(--mono)';
      const subChk = h('div', { class: 'lp-sub' });
      const chkRows = [
        ['Citation', 'quote found in <em>profile</em>: <em>phone: (555) 010-0147</em>'],
        ['Target binding', 'control 6 and point (612, 596) name the same field'],
        ['Secret gate', 'value judged ordinary (0.97); passwords and codes never pass'],
      ].map(([b, sp]) => {
        const ic = h('div', { class: 'ic' });
        const row = h('div', { class: 'chk' }, ic, h('div', null, h('b', null, b), h('span', { html: sp })));
        subChk.append(row);
        return { row, ic };
      });
      const subAct2 = h('div', { class: 'lp-sub' },
        h('div', { class: 'big-ok' }, 'One dispatch, on control 6.'),
        h('div', { class: 'inv', style: { marginTop: '22px' } },
          h('div', null, 'selector     bound to the observed control'),
          h('div', null, 'focus        Phone (input/tel)'),
          h('div', null, 'value        "(555) 010-0147"'),
          h('div', null, 'receiver     the field that actually took the text')));
      const ctrA = h('span', null, '7');
      const subVer = h('div', { class: 'lp-sub' },
        h('div', { class: 'inv' },
          h('div', { class: 'hot', style: { background: 'rgba(94,230,189,.1)', boxShadow: '0 0 0 1.5px rgba(94,230,189,.6)', color: '#d1fae5' } }, 'read-back   "(555) 010-0147"\nexpected    "(555) 010-0147"   ✓ match')),
        h('div', { style: { marginTop: '30px' } }, h('div', { class: 'stepctr' }, ctrA, h('small', null, 'verified steps'))));
      card.append(subInv, subAct, subChk, subAct2, subVer);
      root.append(card);
      this.r = { ch, jp, flash, scan, pointer, pill, rsvg, prog, gate, dot, lab, center, card, dTitle, dRight, subInv, invRows, subAct, json, subChk, chkRows, subAct2, subVer, ctrA, RX, RY, RR, marks: null };
    },
    _layout() {
      // marks on controls, computed once the page is laid out
      const r = this.r;
      const page = r.ch.page;
      const f = r.jp.fields;
      const list = [['first', 2], ['last', 3], ['email', 4], ['cc', 5], ['phone', 6], ['resume', 7], ['auth', 8], ['python', 9]];
      r.marks = list.map(([k, n]) => {
        const p = pos(f[k].inp, page);
        const m = h('div', { class: 'mark', style: { left: (p.x - 6) + 'px', top: (p.y - 6) + 'px', width: (p.w + 12) + 'px', height: (p.h + 12) + 'px' } }, h('b', null, String(n)));
        page.append(m);
        return { m, n, p };
      });
      const sp = pos(r.jp.submit, page);
      const sm = h('div', { class: 'mark', style: { left: (sp.x - 6) + 'px', top: (sp.y - 6) + 'px', width: (sp.w + 12) + 'px', height: (sp.h + 12) + 'px' } }, h('b', null, '12'));
      page.append(sm);
      r.marks.push({ m: sm, n: 12, p: sp });
      r.phone = pos(f.phone.inp, page);
      page.append(r.pointer.el); // keep pointer on top
    },
    update(t, sc) {
      const r = this.r;
      if (!r.marks) this._layout();
      const tHere = LN('l01').start, tObs = WD('l02', 'Observe'), tScreen = WD('l02', 'screen'), tPage = WD('l02', 'page'), tNum = WD('l02', 'numbered'), tEvery = WD('l02', 'every');
      const tDec = WD('l03', 'Decide'), tType = WD('l03', 'type'), tSix = WD('l03', 'six'), tQuote = WD('l03', 'quote');
      const tChk = WD('l04', 'Check'), tFinds = WD('l04', 'finds'), tProf = WD('l04', 'profile'), tConf = WD('l04', 'confirms'), tTarget = WD('l04', 'target'), tScreens = WD('l04', 'screens'), tSecrets = WD('l04', 'secrets');
      const tAct = WD('l05', 'Act'), tDisp = WD('l05', 'dispatch'), tField = WD('l05', 'field');
      const tVer = WD('l06', 'Verify'), tBack = WD('l06', 'back'), tOnly = WD('l06', 'Only'), tCount = WD('l06', 'count');
      const end = sc.end;
      const out = 1 - P(t, end - 0.4, 0.5, E.inQ);
      // entrance
      const kIn = P(t, sc.start + 0.05, 0.9, E.outQ5);
      set(r.ch.el, { o: kIn * out, x: (1 - kIn) * -80 });
      // pill thoughts
      r.pill.update(t, P(t, tHere, 0.5) * out, [
        [tHere, 'The application form is open; checking which fields still need values.'],
        [tType, 'Phone is empty; the profile has it.'],
        [tAct, 'Typing the profile phone number into Phone.'],
        [tVer, 'Phone reads back (555) 010-0147; moving to the next field.'],
      ]);
      // observe: flash + scan + marks
      set(r.flash, { o: 0.35 * (1 - P(t, tObs, 0.35)) * (t >= tObs ? 1 : 0) });
      const ks = P(t, tScreen - 0.2, 1.1, E.ioC);
      set(r.scan, { o: t > tScreen - 0.2 && t < tScreen + 1.0 ? 1 : 0, y: -140 + ks * 1000 });
      r.marks.forEach((mk, i) => {
        const k = P(t, tNum - 0.1 + i * 0.07, 0.4, E.outB);
        const fade = 1 - P(t, tAct - 0.3, 0.5);
        const keep = mk.n === 6 ? 1 - P(t, tVer + 1.2, 0.5) : fade;
        set(mk.m, { o: k * keep, s: 0.9 + 0.1 * k });
        if (mk.n === 6) css(mk.m, 'border-color', t > tSix - 0.05 ? 'rgba(94,230,189,.95)' : 'rgba(85,173,255,.9)');
      });
      // ring state
      const phase = t < tObs ? -1 : t < tDec ? 0 : t < tChk ? 1 : t < tAct ? 2 : t < tVer ? 3 : 4;
      cls(r.lab.obs, 'on', phase === 0); cls(r.lab.dec, 'on', phase === 1); cls(r.lab.chk, 'on', phase === 2); cls(r.lab.act, 'on', phase === 3); cls(r.lab.ver, 'on', phase === 4);
      const kRing = P(t, tHere - 0.2, 0.9, E.outQ5);
      for (const el of [r.rsvg, r.center, ...Object.values(r.lab)]) set(el, { o: kRing * out });
      // progress arc + dot: observe 0 → decide .25 → check .4 → act .5 → verify .75 → 1
      const seg = [[tObs, 0], [tDec, 0.25], [tChk, 0.36], [tAct, 0.5], [tVer, 0.75], [tOnly, 1.0]];
      let a = 0;
      for (let i = 0; i < seg.length - 1; i++) if (t >= seg[i][0]) a = lerp(seg[i][1], seg[i + 1][1], E.ioC(clamp((t - seg[i][0]) / 0.7)));
      if (t >= tOnly) a = 1;
      F.drawPath(r.prog, a);
      const ang = -Math.PI / 2 + a * Math.PI * 2;
      r.dot.setAttribute('cx', (r.RX + r.RR * Math.cos(ang)).toFixed(1));
      r.dot.setAttribute('cy', (r.RY + r.RR * Math.sin(ang)).toFixed(1));
      r.gate.setAttribute('opacity', phase === 2 ? '0.95' : '0.25');
      // detail card
      const kC = P(t, tObs + 0.2, 0.6, E.outQ5);
      set(r.card, { o: kC * out, y: (1 - kC) * 30 });
      const titles = ['control_inventory', 'the model\'s action', 'runtime checks', 'execute', 'verify'];
      text(r.dTitle, titles[Math.max(0, phase)]);
      text(r.dRight, phase === 0 ? 'screenshot 1120 px' : phase === 1 ? 'json' : phase === 2 ? 'before anything moves' : phase === 3 ? 'one activation' : 'read-back');
      const vis = (ph) => env(t, [tObs, tDec, tChk, tAct, tVer][ph] - 0.05, [tDec, tChk, tAct, tVer, 999][ph] + 0.05, 0.35, 0.3);
      set(r.subInv, { o: vis(0) });
      r.invRows.forEach(([d, line], i) => F.typeOn(d, line, t, tPage + 0.4 + i * 0.55, 160, false));
      // decide: json types, then compress for checks
      set(r.subAct, { o: vis(1) });
      r.json.reveal(P(t, tDec + 0.15, 2.6, E.lin));
      r.json.rows.forEach((row, i) => cls(row.row, 'hl', t > tQuote - 0.05 && (i === 5 || i === 6)));
      // check rows
      set(r.subChk, { o: vis(2) });
      const chkT = [[tFinds, tProf + 0.2], [tConf, tTarget + 0.3], [tScreens, tSecrets + 0.25]];
      r.chkRows.forEach((c, i) => {
        const k = P(t, chkT[i][0] - 0.15, 0.45, E.outQ5);
        set(c.row, { o: k, x: (1 - k) * 24 });
        const ok = t >= chkT[i][1];
        cls(c.row, 'ok', ok);
        if (c.ic.__ok !== ok) { c.ic.__ok = ok; c.ic.textContent = ''; c.ic.append(ok ? icon('check', 20, '#5ee6bd', 2.4) : icon('eye', 18, '#8b909a')); }
      });
      // act: pointer to phone, click, value appears
      set(r.subAct2, { o: vis(3) });
      const ph = r.phone;
      const p0 = { x: 760, y: 900 };
      const tArrive = tDisp - 0.05;
      const pp = F.track(t, [[tAct - 0.6, p0.x, p0.y], [tArrive, ph.x + 120, ph.cy + 4]], E.ioC);
      const ptrO = env(t, tAct - 0.7, tVer + 1.6, 0.3, 0.5);
      r.pointer.update(t, pp.x, pp.y, ptrO * out, tArrive + 0.02, t > tArrive + 0.1 && t < tArrive + 1.7 ? 'typing…' : '');
      const typed = t >= tArrive + 0.25;
      cls(ph && r.jp.fields.phone.inp, 'focus', t >= tArrive && t < tVer + 0.9);
      text(r.jp.fields.phone.val, typed ? '(555) 010-0147' : '');
      // verify
      set(r.subVer, { o: vis(4) });
      const vk = env(t, tBack - 0.1, end, 0.3, 0.4);
      css(r.jp.fields.phone.inp, 'box-shadow', vk > 0.01 ? `0 0 0 ${(4 * vk).toFixed(1)}px rgba(94,230,189,${(0.5 * vk).toFixed(2)})` : (t >= tArrive && t < tVer + 0.9 ? '0 0 0 4px rgba(79,140,255,.18)' : 'none'));
      text(r.ctrA, t >= tCount - 0.4 ? '8' : '7');
      set(r.ctrA, { s: t >= tCount - 0.4 && t < tCount ? 1.08 : 1 });
    },
  });
})();
