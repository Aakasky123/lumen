/* Lumen launch film: player. Live playback follows the soundtrack's clock; render mode exposes a
 * frame-exact seek for the video encoder. */
(function () {
  'use strict';
  const F = window.FILM;
  const TL = F.TL;
  const render = !!window.__RENDER__;
  if (render) document.body.classList.add('render');
  F.boot();

  const stage = document.getElementById('stage');
  const vp = document.getElementById('viewport');
  const fit = () => {
    if (render) { stage.style.transform = 'none'; return; }
    stage.style.transform = `scale(${vp.clientWidth / 1920})`;
  };
  window.addEventListener('resize', fit);
  fit();

  const faces = ['600 100px "Inter Tight"', '500 40px "Inter Tight"', '700 40px "Inter Tight"', '400 30px "Inter"', '500 30px "Inter"',
    '600 30px "Inter"', '700 30px "Inter"', '400 20px "JetBrains Mono"', '500 20px "JetBrains Mono"', '700 20px "JetBrains Mono"', '300 60px "Inter Tight"'];
  window.__ready = Promise.all(faces.map((f) => document.fonts.load(f).catch(() => null))).then(() => document.fonts.ready);
  window.__seek = (t) => F.seek(t);
  window.__duration = TL.duration;
  if (render) return;

  // ---------- live player ----------
  const POSTER = 18.9;
  const audio = new Audio();
  audio.preload = 'auto';
  audio.src = 'soundtrack.mp3';
  let playing = false, started = false, clock0 = 0, clockT = 0;
  const now = () => performance.now() / 1000;
  const audioOk = () => !audio.error && audio.readyState >= 2;
  const cur = () => {
    if (!started) return POSTER;
    if (playing && audioOk()) return audio.currentTime;
    if (playing) return clockT + (now() - clock0);
    return clockT;
  };
  const fmt = (t) => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, '0')}`;

  const pp = document.getElementById('pp');
  const playbtn = document.getElementById('playbtn');
  const fill = document.getElementById('fill');
  const time = document.getElementById('time');
  const scrub = document.getElementById('scrub');
  const ccBtn = document.getElementById('cc');
  const fsBtn = document.getElementById('fs');
  const chapNav = document.getElementById('chapters');
  scrub.setAttribute('aria-valuemax', String(Math.round(TL.duration)));
  document.getElementById('playlabel').textContent = `Play film · ${fmt(TL.duration)}`;

  function setPlaying(on) {
    const t = cur();
    playing = on;
    clockT = t; clock0 = now();
    if (on) {
      if (!started) { started = true; clockT = 0; }
      try { audio.currentTime = clockT; } catch (e) { /* not loaded yet */ }
      audio.play().catch(() => { /* fall back to the wall clock */ });
    } else audio.pause();
    pp.textContent = on ? 'Pause' : 'Play';
    pp.setAttribute('aria-label', on ? 'Pause' : 'Play');
    playbtn.hidden = on || started;
  }
  function seekTo(t) {
    started = true;
    playbtn.hidden = true;
    t = Math.max(0, Math.min(TL.duration - 0.05, t));
    clockT = t; clock0 = now();
    try { audio.currentTime = t; } catch (e) { /* ignore */ }
    if (!playing) F.seek(t);
  }

  playbtn.addEventListener('click', () => setPlaying(true));
  pp.addEventListener('click', () => setPlaying(!playing));
  vp.addEventListener('click', (e) => { if (e.target === playbtn || playbtn.contains(e.target)) return; if (started) setPlaying(!playing); });
  ccBtn.addEventListener('click', () => { F.captions = !F.captions; ccBtn.setAttribute('aria-pressed', String(F.captions)); if (!playing) F.seek(cur()); });
  fsBtn.addEventListener('click', () => {
    const el = vp;
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    else if (el.requestFullscreen) el.requestFullscreen().then(fit).catch(() => {});
  });
  document.addEventListener('fullscreenchange', () => setTimeout(fit, 50));
  audio.addEventListener('ended', () => { playing = false; pp.textContent = 'Play'; clockT = TL.duration; });

  const scrubAt = (e) => {
    const r = scrub.getBoundingClientRect();
    seekTo(((e.clientX - r.left) / r.width) * TL.duration);
  };
  let dragging = false;
  scrub.addEventListener('pointerdown', (e) => { dragging = true; scrub.setPointerCapture(e.pointerId); scrubAt(e); });
  scrub.addEventListener('pointermove', (e) => { if (dragging) scrubAt(e); });
  scrub.addEventListener('pointerup', () => { dragging = false; });
  scrub.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowRight') { seekTo(cur() + 5); e.preventDefault(); }
    if (e.key === 'ArrowLeft') { seekTo(cur() - 5); e.preventDefault(); }
  });
  document.addEventListener('keydown', (e) => {
    if (e.code === 'Space' && e.target === document.body) { e.preventDefault(); setPlaying(!playing); }
  });

  // chapter ticks + buttons
  const names = { cold: 'Opening', meet: 'Meet Lumen', close: 'Close' };
  const chapBtns = [];
  for (const sc of TL.scenes) {
    const tick = document.createElement('div');
    tick.className = 'tick';
    tick.style.left = `${(sc.start / TL.duration) * 100}%`;
    scrub.append(tick);
    if (sc.id === 'meet') continue;
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = sc.chapter ? sc.chapter.replace(/^\d+\s·\s/, '') : names[sc.id];
    b.addEventListener('click', () => { seekTo(sc.start); if (!playing) setPlaying(true); });
    chapNav.append(b);
    chapBtns.push([sc, b]);
  }

  function frame() {
    const t = cur();
    if (playing && t >= TL.duration) { setPlaying(false); }
    F.seek(Math.min(t, TL.duration - 0.01));
    fill.style.width = `${started ? (Math.min(t, TL.duration) / TL.duration) * 100 : 0}%`;
    time.textContent = `${fmt(started ? t : 0)} / ${fmt(TL.duration)}`;
    scrub.setAttribute('aria-valuenow', String(Math.round(t)));
    for (const [sc, b] of chapBtns) b.classList.toggle('on', started && t >= sc.start && t < sc.end);
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
})();
