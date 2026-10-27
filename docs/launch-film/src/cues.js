/* Lumen launch film: sound cue sheet. Every cue is derived from the same word anchors the visuals
 * use, so sound effects land on the frame where their event happens. Only used at build time. */
(function () {
  'use strict';
  const F = window.FILM;
  const { WD, WE, LN, SC } = F;
  const cues = [];
  const c = (t, kind, gain = 1, pan = 0, extra = {}) => cues.push(Object.assign({ t: +t.toFixed(4), kind, gain, pan }, extra));

  F.collectCues = function () {
    cues.length = 0;
    // ---------- cold ----------
    {
      const tAI = WD('c02', 'AI'), tEasy = WE('c02', 'part'), tKnow = WD('c03', 'knowing'), tAct = WD('c03', 'actually');
      const n = 16, t0 = tAI - 0.05, t1 = tEasy + 0.15;
      for (let i = 0; i < n; i++) c(t0 + (t1 - t0) * Math.pow(i / (n - 1), 0.72), 'tick', 0.55 + 0.3 * (i / n), ((i * 37) % 9 - 4) / 6, { pitch: 1 + (i % 5) * 0.06 });
      c(tKnow + 0.15, 'click', 0.9, 0.2);
      c(tKnow + 0.4, 'chime_ok', 0.7, 0.35);
      c(tAct - 0.05, 'error', 1.0, 0);
      c(SC('cold').end - 1.25, 'riser', 0.6, 0, { dur: 1.25 });
    }
    // ---------- meet ----------
    {
      for (let i = 0; i < 4; i++) c(15.75 + i * 0.13, 'slab', 0.65, (i - 1.5) / 3);
      c(15.75 + 3 * 0.13 + 0.62, 'impact', 0.85, 0);
      const tA = LN('m02').start;
      c(tA - 0.25, 'whoosh', 0.6, 0);
      c(WD('m02', 'Chrome') - 0.1, 'tick', 0.45, -0.4);
      c(WD('m02', 'Windows') - 0.1, 'tick', 0.45, 0.4);
      const tYou = LN('m03').start;
      for (let i = 0; i < 12; i++) c(tYou + 0.2 + i * 0.065, 'key', 0.22, 0.4);
      c(WD('m03', 'outcome') + 0.6, 'pop', 0.6, 0.4);
      const tVer = WD('m03', 'verified');
      for (let i = 0; i < 4; i++) c(tVer - 0.25 + i * 0.36, 'step', 0.5, 0.35, { pitch: 1 + i * 0.12 });
    }
    // ---------- brain ----------
    {
      c(WD('b02', 'brain') - 0.2, 'whoosh', 0.5, 0);
      c(WD('b02', 'looks') - 0.3, 'tick', 0.4, -0.5);
      c(WD('b02', 'chooses') + 0.1, 'tick', 0.4, 0.5);
      const tAny = WD('b02', 'Any'), tSeat = WE('b02', 'seat');
      const span = Math.max(0.3, (tSeat - tAny) / 7);
      for (let i = 0; i < 8; i++) c(tAny + i * span, 'blip', 0.35, 0.1, { pitch: 1 + (i % 3) * 0.1 });
      c(WD('b03', 'Chrome') - 0.25, 'whoosh', 0.45, -0.4);
      c(WD('b03', 'Windows') - 0.25, 'whoosh', 0.45, 0.4);
      c(WD('b03', 'One') - 0.1, 'swish', 0.5, 0);
      c(WD('b04', 'enumerates') - 0.1, 'sweep_up', 0.5, -0.3);
      c(WD('b04', 'interprets'), 'sweep_down', 0.5, 0.3);
      ['binds', 'executes', 'verifies'].forEach((w, i) => c(WD('b04', w), 'step', 0.6, -0.3 + i * 0.3, { pitch: 1 + i * 0.12 }));
      const tNo = WD('b05', 'No');
      for (let i = 0; i < 3; i++) c(tNo + 0.4 + i * 0.32, 'scratch', 0.5, (i - 1) * 0.2);
      c(WD('b05', 'One') - 0.15, 'ring', 0.7, 0);
    }
    // ---------- loop ----------
    {
      const tObs = WD('l02', 'Observe');
      c(tObs, 'shutter', 0.7, -0.3);
      c(WD('l02', 'screen') - 0.2, 'scan', 0.4, -0.3, { dur: 1.1 });
      const tNum = WD('l02', 'numbered');
      for (let i = 0; i < 9; i++) c(tNum - 0.1 + i * 0.07, 'tick', 0.3, -0.5 + i * 0.05, { pitch: 1.2 + i * 0.03 });
      c(WD('l03', 'Decide'), 'phase', 0.55, 0.4);
      for (let i = 0; i < 14; i++) c(WD('l03', 'Decide') + 0.15 + i * 0.18, 'key', 0.12, 0.45);
      c(WD('l03', 'quote') - 0.05, 'blip', 0.4, 0.4, { pitch: 1.3 });
      c(WD('l04', 'Check'), 'phase', 0.55, 0.4);
      [[WD('l04', 'profile') + 0.2, 1.0], [WD('l04', 'target') + 0.3, 1.12], [WD('l04', 'secrets') + 0.25, 1.26]].forEach(([t, p]) => c(t, 'check', 0.6, 0.4, { pitch: p }));
      c(WD('l05', 'Act'), 'phase', 0.55, 0);
      c(WD('l05', 'dispatch') - 0.03, 'click', 0.9, -0.2);
      c(WD('l06', 'Verify'), 'phase', 0.55, 0);
      c(WD('l06', 'back'), 'chime_ok', 0.75, -0.1);
      c(WD('l06', 'count') - 0.4, 'step', 0.6, 0.4, { pitch: 1.5 });
    }
    // ---------- receipts ----------
    {
      c(SC('receipts').start + 0.1, 'whoosh', 0.45, -0.2);
      ['attempted', 'executed', 'verified'].forEach((w, i) => c(WD('r01', w), 'step', 0.55, -0.3, { pitch: 1 + i * 0.12 }));
      c(WD('r01', 'outcome') - 0.1, 'chime_ok', 0.7, -0.2);
      c(WD('r01', 'model') - 0.1, 'blip', 0.35, 0.4, { pitch: 0.8 });
      c(WD('r01', 'never'), 'deny', 0.8, 0.4);
      c(WD('r02', 'Opening') - 0.2, 'whoosh', 0.4, -0.3);
      c(WD('r02', 'uncertain') - 0.3, 'whoosh', 0.4, 0.3);
      c(WD('r02', 'looks'), 'scan', 0.35, 0.3, { dur: 0.8 });
      c(WD('r02', 'twice') - 0.5, 'tick', 0.5, 0.3);
    }
    // ---------- ground ----------
    {
      c(WD('g01', 'guess') - 0.3, 'blip', 0.35, -0.3, { pitch: 0.9 });
      c(WD('g01', 'facts') - 0.05, 'deny', 0.8, -0.3);
      c(WD('g02', 'comes') - 0.35, 'pop', 0.6, 0.4);
      c(WD('g02', 'Optional'), 'tick', 0.35, -0.3);
      c(WD('g03', 'Tell') + 0.1, 'pop', 0.55, 0.45);
      c(WD('g03', 'answer'), 'check', 0.55, -0.3);
      c(WD('g03', 'memory') + 0.3, 'chime_ok', 0.7, 0.3);
    }
    // ---------- floors ----------
    {
      c(WE('f01', 'floors') - 0.02, 'impact', 0.8, 0, { low: true });
      const tModel = WD('f02', 'model');
      for (let i = 0; i < 22; i++) c(tModel + i * 0.025, 'tick', 0.08, 0.4, { pitch: 1.4 });
      c(WD('f02', 'never') + 0.1, 'deny', 0.6, 0.4);
      c(WD('f02', 'Before') + 0.85, 'click_block', 0.8, -0.3);
      c(WD('f02', 'judge'), 'blip', 0.35, 0.4, { pitch: 0.8 });
      c(WD('f02', 'confident'), 'sweep_down', 0.35, 0.4);
      c(WD('f03', 'stops') - 0.35, 'pop', 0.65, 0.4);
      c(WD('f03', 'bound') - 0.1, 'sweep_up', 0.35, 0);
      c(WD('f03', 'button') + 0.3, 'click', 0.8, 0.4);
      c(WD('f04', 'Full'), 'pop', 0.45, 0.4);
      c(WD('f04', 'fires') - 0.05, 'click', 1.0, -0.3);
      c(WD('f04', 'once') + 0.1, 'success', 0.85, -0.2);
      c(WD('f05', 'Password') - 0.3, 'whoosh', 0.45, 0);
      c(WD('f05', 'blacked') - 0.1, 'shutter', 0.7, 0.35);
      c(WD('f06', 'desktop') - 0.3, 'whoosh', 0.45, 0);
      c(WD('f06', 'shells') - 0.2, 'deny', 0.5, 0.4);
      c(WD('f06', 'pause') + 0.05, 'power_down', 0.8, 0.3);
    }
    // ---------- cross ----------
    {
      const tHere = LN('x02').start, tEnters = WD('x02', 'enters'), tRef = WD('x02', 'refuses'), tOver = WD('x02', 'overwrite'), tCar = WD('x02', 'carries'), tClip = WD('x02', 'clipboard');
      [tHere + 0.4, tEnters - 0.25, tRef - 0.4, tClip].forEach((t, i) => c(t, 'swish', 0.55, i % 2 ? -0.4 : 0.4));
      c(tEnters + 0.05, 'click', 0.7, -0.4);
      c(tRef + 0.12, 'click', 0.7, 0.3);
      c(tRef + 0.18, 'dialog', 0.6, 0.3);
      c(tOver + 1.25, 'click', 0.8, 0.35);
      c(tCar + 0.32, 'click', 0.7, 0.3);
      c(tCar + 0.45, 'whoosh', 0.4, 0, { dur: 0.9 });
      c(tClip + 0.42, 'click', 0.6, -0.3);
      c(LN('x02').end, 'chime_ok', 0.6, -0.3);
    }
    // ---------- proof ----------
    {
      const tG = WD('p01', 'Gauntlet');
      for (let i = 0; i < 28; i++) c(tG + i * 0.035, 'tick', 0.18, -0.7 + (i % 14) * 0.1, { pitch: 1 + (i % 7) * 0.05 });
      ['Labels', 'Instructions', 'Keyboards', 'Dialogs'].forEach((w) => c(WD('p02', w) - 0.25, 'trap', 0.6, 0));
      const tPassed = WD('p03', 'passed'), tSix = WD('p03', 'six');
      for (let i = 0; i < 21; i++) c(tPassed + i * 0.02 + (i % 3) * 0.004, 'check', 0.16, -0.6 + (i % 12) * 0.1, { pitch: 1 + (i % 6) * 0.08 });
      for (let i = 0; i < 6; i++) c(tSix + (21 + i) * 0.02, 'check', 0.2, 0.6, { pitch: 1.3 });
      c(WD('p03', 'twenty-one') - 0.2, 'hit', 0.8, 0);
      const tReal = WD('p04', 'real');
      for (let i = 0; i < 3; i++) c(tReal + 0.2 + i * 0.45, 'whoosh', 0.35, -0.5 + i * 0.5);
      const tF = WD('p04', 'failures');
      for (let i = 0; i < 8; i++) c(tF + i * 0.12, 'ratchet', 0.55, -0.5 + i * 0.14);
    }
    // ---------- honest ----------
    {
      c(WD('h02', 'thirty') - 0.2, 'grow', 0.4, -0.1, { dur: 1.0 });
      c(WD('h02', 'leaders') - 0.5, 'grow', 0.3, 0.1, { dur: 1.0 });
      c(WD('h03', 'frontier') + 0.2, 'swap', 0.6, -0.3);
      const tP = WD('h03', 'passed');
      for (let i = 0; i < 8; i++) c(tP + i * 0.12, 'check', 0.45, 0.2 + i * 0.05, { pitch: 1 + i * 0.07 });
      c(WD('h03', 'failed') - 0.2, 'chime_ok', 0.6, 0.4);
    }
    // ---------- close ----------
    {
      const tEvery = WD('z01', 'every');
      for (let i = 0; i < 7; i++) c(tEvery - 0.4 + i * 0.42 + 1.1, 'blip', 0.25, (i % 2 ? 0.5 : -0.5), { pitch: 1 + i * 0.06 });
      c(WD('z02', 'Lumen') - 0.55, 'impact', 0.7, 0, { final: true });
    }
    cues.sort((a, b) => a.t - b.t);
    return cues.slice();
  };
})();
