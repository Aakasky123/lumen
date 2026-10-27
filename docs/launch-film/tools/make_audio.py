"""Score, sound design, and mix for the Lumen film. Pure numpy/scipy synthesis: no samples.

Reads build/timeline.json, build/cues.json, build/vo/*.wav. Writes
build/audio/{music,sfx,vo,mix}.wav and out/soundtrack.mp3 (+ .m4a for the video mux).
"""

import json
import math
import pathlib
import subprocess

import imageio_ffmpeg
import numpy as np
import scipy.signal as sg
from scipy.io import wavfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
B = ROOT / "build"
SR = 48000
FF = imageio_ffmpeg.get_ffmpeg_exe()
RNG = np.random.default_rng(20261001)

TL = json.loads((B / "timeline.json").read_text(encoding="utf-8"))
CUES = json.loads((B / "cues.json").read_text(encoding="utf-8"))
DUR = TL["duration"]
N = int(DUR * SR)
BEAT = TL["beat"]
SCENES = {s["id"]: s for s in TL["scenes"]}


# ---------------------------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------------------------
def mtof(m: float) -> float:
    return 440.0 * 2.0 ** ((m - 69) / 12.0)


def tt(dur: float) -> np.ndarray:
    return np.arange(int(dur * SR)) / SR


def sos(kind: str, f, order: int = 2):
    return sg.butter(order, f, btype=kind, fs=SR, output="sos")


def filt(x: np.ndarray, kind: str, f, order: int = 2) -> np.ndarray:
    return sg.sosfilt(sos(kind, f, order), x, axis=0)


def adsr(n: int, a: float, d: float, s: float, r: float) -> np.ndarray:
    a_n, d_n, r_n = int(a * SR), int(d * SR), int(r * SR)
    s_n = max(0, n - a_n - d_n - r_n)
    parts = [np.linspace(0, 1, max(1, a_n), endpoint=False), np.linspace(1, s, max(1, d_n), endpoint=False),
             np.full(s_n, s), np.linspace(s, 0, max(1, r_n))]
    e = np.concatenate(parts)
    if len(e) < n:
        e = np.pad(e, (0, n - len(e)))
    return e[:n]


def pan2(x: np.ndarray, pan: float) -> np.ndarray:
    a = (pan + 1) * math.pi / 4
    return np.stack([x * math.cos(a), x * math.sin(a)], axis=1)


def place(buf: np.ndarray, sig: np.ndarray, t0: float) -> None:
    i0 = int(round(t0 * SR))
    if i0 >= len(buf) or len(sig) == 0:
        return
    if i0 < 0:
        sig = sig[-i0:]
        i0 = 0
    n = min(len(sig), len(buf) - i0)
    buf[i0:i0 + n] += sig[:n]


def saw(f: float, t: np.ndarray, phase: float = 0.0, bright: float = 1.0) -> np.ndarray:
    """band-limited saw by additive synthesis; bright < 1 tilts harmonics down"""
    out = np.zeros_like(t)
    k = 1
    while k * f < 9000 and k <= 28:
        out += np.sin(2 * np.pi * k * f * t + phase * k) / (k ** (1.0 + 0.6 * (1 - bright)))
        k += 1
    return out


def make_ir(rt_low: float = 3.4, rt_high: float = 1.6, seed: int = 3) -> np.ndarray:
    n = int(max(rt_low, rt_high) * 1.15 * SR)
    t = np.arange(n) / SR
    rng = np.random.default_rng(seed)
    chans = []
    for ch in range(2):
        noise = rng.standard_normal(n)
        lo = filt(noise, "low", 900) * np.exp(-6.9 * t / rt_low)
        hi = filt(noise, "high", 900) * np.exp(-6.9 * t / rt_high)
        ir = lo + 0.6 * hi
        pre = int(0.022 * SR)
        ir = np.concatenate([np.zeros(pre), ir])[:n]
        for d_ms, g in ((11, 0.5), (19, 0.35), (29, 0.25), (41, 0.18)):  # a few early reflections
            j = int((d_ms + 7 * ch) / 1000 * SR)
            ir[j] += g
        chans.append(ir)
    ir = np.stack(chans, axis=1)
    return ir / np.sqrt((ir ** 2).sum(axis=0, keepdims=True)) * 0.9


IR = make_ir()
IR_SHORT = make_ir(1.1, 0.6, seed=9)


def reverb(x: np.ndarray, ir: np.ndarray = IR) -> np.ndarray:
    if x.ndim == 1:
        x = np.stack([x, x], axis=1)
    out = np.stack([sg.fftconvolve(x[:, c], ir[:, c])[: len(x)] for c in range(2)], axis=1)
    return out


def rms_db(x: np.ndarray) -> float:
    return 20 * math.log10(max(1e-9, float(np.sqrt(np.mean(x ** 2)))))


# ---------------------------------------------------------------------------------------------
# instruments
# ---------------------------------------------------------------------------------------------
def pad_chord(notes, dur: float, att: float = 1.6, rel: float = 2.2, bright: float = 0.55, detune: float = 0.11) -> np.ndarray:
    t = tt(dur + rel)
    out = np.zeros((len(t), 2))
    for i, m in enumerate(notes):
        f = mtof(m)
        for j, dc in enumerate((-detune, 0.0, detune)):
            ff = f * 2 ** (dc / 12)
            ph = RNG.uniform(0, 2 * np.pi)
            v = saw(ff, t, ph, bright)
            out += pan2(v, (-0.55, 0.0, 0.55)[j] * (0.6 + 0.4 * ((i % 2) * 2 - 1)))
    out /= max(1, len(notes) * 3)
    # slow filter swell + amplitude envelope
    cut = 900 + 1700 * bright
    out = filt(out, "low", cut, 2)
    e = adsr(len(t), att, 0.8, 0.85, rel)
    lfo = 1 + 0.06 * np.sin(2 * np.pi * 0.17 * t)
    return out * (e * lfo)[:, None]


def pluck(f: float, dur: float = 0.9, bright: float = 0.6) -> np.ndarray:
    t = tt(dur)
    out = np.zeros_like(t)
    for k in range(1, 9):
        if k * f > 12000:
            break
        out += np.sin(2 * np.pi * k * f * t) * (0.62 ** (k - 1)) * np.exp(-t * (5.5 + 3.2 * k * (1.2 - bright)))
    att = np.minimum(1, t / 0.004)
    return out * att * 0.5


def bell(f: float, dur: float = 2.6, idx: float = 2.2, ratio: float = 3.5) -> np.ndarray:
    t = tt(dur)
    mod = np.sin(2 * np.pi * f * ratio * t) * idx * np.exp(-t * 2.5)
    out = np.sin(2 * np.pi * f * t + mod) * np.exp(-t * 1.9) * np.minimum(1, t / 0.003)
    return out * 0.45


def epiano(f: float, dur: float = 2.8) -> np.ndarray:
    t = tt(dur)
    mod = np.sin(2 * np.pi * f * t) * 1.6 * np.exp(-t * 3.0)
    out = np.sin(2 * np.pi * f * t + mod) * np.exp(-t * 1.1)
    out += 0.25 * np.sin(2 * np.pi * 2 * f * t) * np.exp(-t * 2.5)
    out *= np.minimum(1, t / 0.006) * (1 + 0.08 * np.sin(2 * np.pi * 4.6 * t))
    return out * 0.4


def bass_note(f: float, dur: float, pulse: bool = False) -> np.ndarray:
    t = tt(dur)
    x = np.sin(2 * np.pi * f * t) + 0.25 * np.sin(2 * np.pi * 2 * f * t) + 0.08 * np.sin(2 * np.pi * 3 * f * t)
    x = np.tanh(1.3 * x) / np.tanh(1.3)
    e = adsr(len(t), 0.012 if pulse else 0.3, 0.2, 0.75, 0.25 if pulse else 0.8)
    return x * e * 0.55


def kick(gain: float = 1.0) -> np.ndarray:
    t = tt(0.45)
    f = 45 + 75 * np.exp(-t * 28)
    ph = 2 * np.pi * np.cumsum(f) / SR
    x = np.sin(ph) * np.exp(-t * 7.5)
    click = filt(RNG.standard_normal(len(t)), "high", 1500) * np.exp(-t * 180) * 0.25
    return (x + click) * gain


def hat(gain: float = 1.0, open_: bool = False) -> np.ndarray:
    t = tt(0.22 if open_ else 0.06)
    x = filt(RNG.standard_normal(len(t)), "high", 7000) * np.exp(-t * (18 if open_ else 70))
    return x * gain * 0.35


def drone(f: float, dur: float) -> np.ndarray:
    t = tt(dur)
    x = np.sin(2 * np.pi * f * t) + 0.5 * np.sin(2 * np.pi * f * 1.5 * t + 0.4) + 0.3 * np.sin(2 * np.pi * f * 2.003 * t)
    tex = filt(RNG.standard_normal(len(t)), "band", [180, 900]) * 0.12
    x = (x * 0.35 + tex) * (1 + 0.15 * np.sin(2 * np.pi * 0.11 * t))
    return pan2(x, 0) + pan2(filt(RNG.standard_normal(len(t)), "band", [300, 1400]) * 0.03, 0.4)


# chord voicings (MIDI)
CH = {
    "Dmaj9": [50, 57, 61, 64, 66], "Bm9": [47, 54, 57, 61, 62], "Gmaj9": [43, 50, 54, 57, 59],
    "Asus": [45, 52, 59, 62, 64], "Em9": [40, 47, 50, 54, 55], "F#m7": [42, 49, 52, 57, 61], "D/F#": [42, 50, 57, 62, 64],
}
ROOT_NOTE = {"Dmaj9": 38, "Bm9": 35, "Gmaj9": 31, "Asus": 33, "Em9": 28, "F#m7": 30, "D/F#": 30}

PLAN = {
    #  scene: (progression, pad, arp, bass, kick, hat, bell, piano, drone)
    "meet": (["Dmaj9", "Gmaj9", "Bm9", "Asus"], 0.9, 0.55, 0.45, 0.0, 0.0, 0.5, 0.0, 0.0),
    "brain": (["Gmaj9", "D/F#", "Em9", "Asus"], 0.75, 0.6, 0.55, 0.32, 0.28, 0.25, 0.0, 0.0),
    "loop": (["Bm9", "Gmaj9", "D/F#", "Asus"], 0.75, 0.72, 0.62, 0.5, 0.42, 0.25, 0.0, 0.0),
    "receipts": (["Gmaj9", "Asus", "Bm9", "D/F#"], 0.75, 0.55, 0.55, 0.36, 0.32, 0.35, 0.0, 0.0),
    "ground": (["Dmaj9", "Gmaj9", "Em9", "Asus"], 0.75, 0.45, 0.4, 0.0, 0.2, 0.3, 0.0, 0.0),
    "floors": (["Bm9", "Gmaj9", "F#m7", "Bm9"], 0.85, 0.25, 0.55, 0.0, 0.0, 0.0, 0.0, 0.35),
    "cross": (["Em9", "Gmaj9", "Asus", "Bm9"], 0.7, 0.75, 0.58, 0.46, 0.42, 0.2, 0.0, 0.0),
    "proof": (["Gmaj9", "Asus", "Bm9", "D/F#"], 0.85, 0.85, 0.72, 0.62, 0.5, 0.35, 0.0, 0.0),
    "honest": (["Bm9", "Gmaj9", "Em9", "Asus"], 0.45, 0.0, 0.25, 0.0, 0.0, 0.0, 0.75, 0.0),
    "close": (["Gmaj9", "Asus", "Dmaj9", "Dmaj9"], 0.95, 0.5, 0.6, 0.0, 0.15, 0.45, 0.35, 0.0),
}


def compose():
    pad = np.zeros((N, 2)); arp = np.zeros((N, 2)); bass = np.zeros((N, 2)); drums = np.zeros((N, 2)); bells = np.zeros((N, 2))
    keys = np.zeros((N, 2)); drn = np.zeros((N, 2))
    bar = 4 * BEAT
    # cold open: a dark drone that tightens, then a hard cut to silence before the reveal
    cold = SCENES["cold"]
    d = drone(mtof(35), cold["end"] - 0.2) * 0.5
    d *= np.minimum(1, np.arange(len(d)) / (2.4 * SR))[:, None]
    d *= np.clip((cold["end"] - 0.2 - np.arange(len(d)) / SR) / 0.05, 0, 1)[:, None]
    place(drn, d, 0.0)
    # ticking clock under "endless" (16ths), speeding up with the clicks
    tick_t = 2.4
    while tick_t < cold["end"] - 1.3:
        g = 0.05 + 0.05 * min(1, max(0, (tick_t - 7.0) / 3.0))
        x = np.sin(2 * np.pi * 2400 * tt(0.02)) * np.exp(-tt(0.02) * 300) * g
        place(drums, pan2(x, 0.3 if int(tick_t * 8) % 2 else -0.3), tick_t)
        tick_t += BEAT / 2 if tick_t < 7.0 else BEAT / 4
    for sid, (prog, g_pad, g_arp, g_bass, g_kick, g_hat, g_bell, g_keys, g_drone) in PLAN.items():
        sc = SCENES[sid]
        s0, s1 = sc["start"], sc["end"]
        if sid == "meet":
            s0 = TL["lines"]["m01"]["words"][2]["t"] - 0.02  # the chord blooms on "Lumen"
        n_ch = len(prog)
        chord_len = 2 * bar
        k = 0
        t = s0
        while t < s1 - 0.05:
            name = prog[k % n_ch]
            dur = min(chord_len, s1 - t)
            if sid == "close" and k >= 2:
                dur = max(dur, (TL["duration"] + 2.0) - t)  # final chord rings out
            notes = CH[name]
            if g_pad:
                p = pad_chord(notes, dur, att=1.2 if k else (0.08 if sid in ("meet",) else 0.9), rel=2.4, bright=0.45 + 0.25 * (sid in ("proof", "close")))
                place(pad, p * g_pad, t)
            if g_bass:
                root = mtof(ROOT_NOTE[name])
                if g_kick and sid != "floors":
                    for b in range(int(dur / BEAT)):
                        place(bass, pan2(bass_note(root, BEAT * 0.95, pulse=True), 0) * g_bass * (0.85 if b % 2 else 1.0), t + b * BEAT)
                else:
                    place(bass, pan2(bass_note(root, dur), 0) * g_bass, t)
            if g_bell and k % 2 == 0:
                place(bells, pan2(bell(mtof(notes[-1] + 12)), 0.35 if k % 4 else -0.35) * g_bell * 0.5, t + 0.02)
            if g_keys:
                for i, m in enumerate(notes[1:]):
                    place(keys, pan2(epiano(mtof(m + 12)), -0.3 + 0.15 * i) * g_keys * 0.5, t + 0.018 * i)
                place(keys, pan2(epiano(mtof(notes[1] + 12), 2.2), 0.2) * g_keys * 0.3, t + bar)
            if g_drone:
                place(drn, drone(mtof(ROOT_NOTE[name]), dur) * g_drone * 0.5, t)
            k += 1
            t += chord_len
        # arpeggio: 8th notes in quieter scenes, 16ths when driving
        if g_arp:
            step = BEAT / 2 if sid in ("meet", "ground", "floors", "close") else BEAT / 4
            a0 = s0 if sid != "meet" else TL["lines"]["m02"]["start"]
            if sid == "brain":
                a0 = s0 + bar
            pattern = [0, 2, 1, 3, 2, 4, 3, 1]
            i = 0
            t = a0
            while t < s1 - 0.1:
                kk = int((t - s0) // chord_len)
                notes = sorted(CH[prog[kk % n_ch]][1:])
                m = notes[pattern[i % len(pattern)] % len(notes)] + 12
                ramp = min(1.0, (t - a0) / (2 * bar) + 0.35)
                fade = min(1.0, max(0.0, (s1 - t) / 0.8))
                vel = (0.75 + 0.25 * ((i % 4) == 0)) * ramp * fade
                place(arp, pan2(pluck(mtof(m), 0.7, 0.55 + 0.3 * (sid in ("proof", "cross"))), 0.45 * math.sin(i * 0.9)) * g_arp * vel * 0.55, t)
                i += 1
                t += step
        # drums
        if g_kick or g_hat:
            t = s0
            b = 0
            while t < s1 - 0.1:
                fade = min(1.0, max(0.0, (s1 - t) / 0.6))
                if g_kick:
                    place(drums, pan2(kick(0.9), 0) * g_kick * fade, t)
                if g_hat:
                    place(drums, pan2(hat(1.0), 0.25) * g_hat * fade, t + BEAT / 2)
                    if sid == "proof" and b % 4 == 3:
                        place(drums, pan2(hat(1.0, True), -0.2) * g_hat * 0.8 * fade, t + 3 * BEAT / 4)
                b += 1
                t += BEAT
        if sid == "floors":  # a heartbeat under the safety section
            f2 = TL["lines"]["f02"]["start"]
            t = f2
            while t < TL["lines"]["f04"]["start"]:
                place(drums, pan2(kick(0.8), 0) * 0.45, t)
                place(drums, pan2(kick(0.6), 0) * 0.3, t + 0.28)
                t += 2 * BEAT
    # arp echoes: dotted-eighth ping-pong
    dly = int(0.75 * BEAT * SR)
    echo = np.zeros_like(arp)
    src = filt(arp, "low", 3500)
    for n, g in ((1, 0.32), (2, 0.18), (3, 0.1)):
        sh = np.zeros_like(arp)
        sh[n * dly:] = src[:-n * dly]
        if n % 2:
            sh = sh[:, ::-1]
        echo += sh * g
    arp = arp + echo
    stems = {"pad": pad, "arp": arp, "bass": bass, "drums": drums, "bells": bells, "keys": keys, "drone": drn}
    return stems


# ---------------------------------------------------------------------------------------------
# sound design
# ---------------------------------------------------------------------------------------------
def mixin(out: np.ndarray, sig: np.ndarray, j: int, g: float = 1.0) -> None:
    n = min(len(sig), len(out) - j)
    if n > 0:
        out[j:j + n] += sig[:n] * g


def nz(n):
    return RNG.standard_normal(n)


def sfx(kind: str, c: dict) -> np.ndarray:
    p = c.get("pitch", 1.0)
    if kind == "tick":
        t = tt(0.05)
        x = np.sin(2 * np.pi * 2200 * p * t) * np.exp(-t * 120) + filt(nz(len(t)), "high", 4000) * np.exp(-t * 400) * 0.3
        return x * 0.5
    if kind == "key":
        t = tt(0.03)
        return filt(nz(len(t)), "band", [1800 * p, 5200]) * np.exp(-t * 220) * 0.6
    if kind == "click":
        t = tt(0.12)
        x = filt(nz(len(t)), "band", [1500, 6500]) * (np.exp(-t * 500) + 0.6 * np.exp(-np.maximum(0, t - 0.055) * 600) * (t > 0.055))
        x += np.sin(2 * np.pi * 180 * t) * np.exp(-t * 60) * 0.5
        return x * 0.7
    if kind == "pop":
        t = tt(0.09)
        f = 500 + 500 * (t / 0.09)
        return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 40) * 0.5
    if kind == "blip":
        t = tt(0.07)
        return np.sin(2 * np.pi * 1250 * p * t) * np.exp(-t * 60) * 0.45
    if kind == "step":
        f = 880 * p
        t = tt(0.4)
        x = (np.sin(2 * np.pi * f * t) + 0.3 * np.sin(2 * np.pi * 4 * f * t) * np.exp(-t * 30)) * np.exp(-t * 9)
        return x * 0.4
    if kind == "check":
        f = 1318.5 * p
        t = tt(0.25)
        return (np.sin(2 * np.pi * f * t) + 0.2 * np.sin(2 * np.pi * 2 * f * t)) * np.exp(-t * 18) * 0.38
    if kind == "phase":
        t = tt(0.32)
        return (np.sin(2 * np.pi * 880 * t) + 0.7 * np.sin(2 * np.pi * 1318.5 * t)) * np.exp(-t * 10) * 0.3 * np.minimum(1, t / 0.004)
    if kind == "chime_ok":
        out = np.zeros(int(0.9 * SR))
        for i, m in enumerate((81, 86)):
            b = bell(mtof(m), 0.9, idx=0.8, ratio=2.0)
            j = int(i * 0.07 * SR)
            n = min(len(b), len(out) - j)
            out[j:j + n] += b[:n]
        return out * 0.8
    if kind == "success":
        out = np.zeros(int(1.4 * SR))
        for i, m in enumerate((74, 78, 81, 86)):
            b = bell(mtof(m), 1.2, idx=0.9, ratio=2.0)
            j = int(i * 0.09 * SR)
            n = min(len(b), len(out) - j)
            out[j:j + n] += b[:n] * (0.8 if i < 3 else 1.0)
        return out * 0.75
    if kind == "error":
        t = tt(0.55)
        x = (np.sign(np.sin(2 * np.pi * 196 * t)) * 0.4 + np.sin(2 * np.pi * 207.7 * t)) * np.exp(-t * 5)
        return filt(x, "low", 1400) * 0.55
    if kind == "deny":
        t = tt(0.22)
        x = np.sin(2 * np.pi * 150 * t) * np.exp(-t * 18) + filt(nz(len(t)), "low", 900) * np.exp(-t * 40) * 0.4
        return x * 0.7
    if kind == "click_block":
        a = sfx("click", c)
        b = sfx("deny", c)
        out = np.zeros(int(0.5 * SR))
        mixin(out, a, 0)
        mixin(out, b, int(0.09 * SR))
        return out
    if kind in ("whoosh", "swish", "sweep_up", "sweep_down", "slab"):
        dur = {"whoosh": c.get("dur", 0.6), "swish": 0.3, "sweep_up": 0.6, "sweep_down": 0.6, "slab": 0.45}[kind]
        n = int(dur * SR)
        x = nz(n)
        out = np.zeros(n)
        blocks = 24
        for i in range(blocks):
            a, b2 = i * n // blocks, (i + 1) * n // blocks
            k = i / (blocks - 1)
            if kind == "sweep_down":
                k = 1 - k
            fc = (300 + 4000 * k) if kind != "slab" else (120 + 700 * k)
            out[a:b2] = filt(x[a:b2], "band", [fc * 0.6, fc * 1.4])
        env = np.sin(np.pi * np.linspace(0, 1, n)) ** 1.5
        return out * env * (0.5 if kind != "slab" else 0.9)
    if kind == "scratch":
        t = tt(0.26)
        return filt(nz(len(t)), "band", [2500, 7000]) * np.exp(-t * 14) * np.minimum(1, t / 0.02) * 0.45
    if kind == "ring":
        a = sfx("whoosh", {"dur": 1.0})
        b = bell(mtof(86), 1.8, 1.0, 2.0)
        out = np.zeros(int(2.0 * SR))
        mixin(out, a, 0, 0.6)
        mixin(out, b, int(0.85 * SR), 0.6)
        return out
    if kind == "shutter":
        t = tt(0.14)
        x = filt(nz(len(t)), "band", [900, 5000]) * (np.exp(-t * 300) + 0.8 * (t > 0.06) * np.exp(-np.maximum(0, t - 0.06) * 280))
        return x * 0.55
    if kind == "scan":
        dur = c.get("dur", 1.0)
        t = tt(dur)
        f = 600 + 500 * t / dur
        x = np.sin(2 * np.pi * np.cumsum(f) / SR) * (0.5 + 0.5 * np.sin(2 * np.pi * 18 * t)) * np.sin(np.pi * t / dur)
        return x * 0.12
    if kind == "impact":
        dur = 3.5 if c.get("final") else 2.4
        t = tt(dur)
        f = 32 + 40 * np.exp(-t * 6)
        sub = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * (1.2 if c.get("final") else 1.8))
        body = filt(nz(len(t)), "low", 500) * np.exp(-t * 9) * 0.5
        x = sub + body
        if c.get("final"):
            for m in (62, 69, 74, 78, 81):
                b = bell(mtof(m), 3.5, 0.6, 2.0)
                mixin(x, b, 0, 0.22)
        return x * 0.9
    if kind == "hit":
        t = tt(1.2)
        f = 40 + 50 * np.exp(-t * 9)
        return (np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 3.5) + filt(nz(len(t)), "band", [200, 2500]) * np.exp(-t * 25) * 0.4) * 0.8
    if kind == "riser":
        dur = c.get("dur", 1.2)
        n = int(dur * SR)
        x = nz(n)
        out = np.zeros(n)
        blocks = 30
        for i in range(blocks):
            a, b2 = i * n // blocks, (i + 1) * n // blocks
            fc = 300 * (20 ** (i / (blocks - 1)))
            out[a:b2] = filt(x[a:b2], "band", [fc * 0.7, min(20000, fc * 1.3)])
        t = np.arange(n) / SR
        tone = np.sin(2 * np.pi * np.cumsum(180 + 600 * (t / dur) ** 2) / SR) * 0.25
        return (out + tone) * (t / dur) ** 2.2 * 0.8
    if kind == "power_down":
        t = tt(0.7)
        f = 620 * np.exp(-t * 3.6) + 60
        return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 4) * 0.45
    if kind == "dialog":
        t = tt(0.5)
        x = np.sin(2 * np.pi * 880 * t) * np.exp(-t * 9) + 0.8 * np.sin(2 * np.pi * 660 * t) * (t > 0.11) * np.exp(-np.maximum(0, t - 0.11) * 9)
        return x * 0.3
    if kind == "trap":
        t = tt(0.4)
        x = np.sin(2 * np.pi * 220 * t) * ((t < 0.08) + (t > 0.14) * (t < 0.22)) * np.exp(-t * 4)
        return filt(x, "low", 2000) * 0.45 + sfx("whoosh", {"dur": 0.4})[: len(t)] * 0.3
    if kind == "ratchet":
        t = tt(0.09)
        x = filt(nz(len(t)), "band", [2500, 6000]) * np.exp(-t * 260) + np.sin(2 * np.pi * 3100 * t) * np.exp(-t * 90) * 0.3
        return x * 0.6
    if kind == "grow":
        dur = c.get("dur", 1.0)
        t = tt(dur)
        f = 300 + 300 * t / dur
        return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.sin(np.pi * t / dur) * 0.18
    if kind == "swap":
        a = sfx("whoosh", {"dur": 0.5})
        b = pluck(mtof(86), 0.6, 0.9)
        out = np.zeros(int(0.9 * SR))
        mixin(out, a, 0)
        mixin(out, b, int(0.35 * SR), 0.8)
        return out
    raise ValueError(kind)


def build_sfx() -> np.ndarray:
    buf = np.zeros((N, 2))
    for c in CUES:
        x = sfx(c["kind"], c)
        place(buf, pan2(x, float(c.get("pan", 0))) * float(c.get("gain", 1)), float(c["t"]))
    return buf


# ---------------------------------------------------------------------------------------------
# narration
# ---------------------------------------------------------------------------------------------
def build_vo() -> np.ndarray:
    buf = np.zeros(N)
    for lid, l in TL["lines"].items():
        sr, x = wavfile.read(B / "vo" / f"{lid}.wav")
        x = x.astype(np.float64)
        a, b = int(l["clip_in"] * sr), int(l["clip_out"] * sr)
        seg = x[a:b].copy()
        fade_in, fade_out = int(0.012 * sr), int(0.06 * sr)
        seg[:fade_in] *= np.linspace(0, 1, fade_in)
        seg[-fade_out:] *= np.linspace(1, 0, fade_out)
        # level each line to the same speech RMS
        voiced = seg[np.abs(seg) > 0.01]
        r = np.sqrt(np.mean(voiced ** 2)) if len(voiced) else 0.1
        seg *= 0.12 / r
        place(buf, seg, l["clip_start"] + l["clip_in"])
    # voice chain: rumble cut, gentle low-mid dip, presence lift, soft compression
    buf = filt(buf, "high", 75, 2)
    low_mid = filt(buf, "band", [220, 420])
    pres = filt(buf, "band", [2800, 5500])
    buf = buf - 0.18 * low_mid + 0.28 * pres
    env = np.sqrt(sg.lfilter([1 - 0.995], [1, -0.995], buf ** 2))
    thr = 0.09
    gain = np.where(env > thr, (thr / np.maximum(env, 1e-9)) ** (1 - 1 / 2.5), 1.0)
    return buf * gain


def smooth_env(x: np.ndarray, att: float, rel: float) -> np.ndarray:
    a = math.exp(-1 / (att * SR))
    r = math.exp(-1 / (rel * SR))
    out = np.zeros_like(x)
    prev = 0.0
    # vectorize by decimating: envelope at 1 kHz then upsample
    dec = 48
    xs = x[: len(x) // dec * dec].reshape(-1, dec).max(axis=1)
    a2, r2 = a ** dec, r ** dec
    env = np.zeros_like(xs)
    for i, v in enumerate(xs):
        coef = a2 if v > prev else r2
        prev = coef * prev + (1 - coef) * v
        env[i] = prev
    out[: len(env) * dec] = np.repeat(env, dec)
    out[len(env) * dec:] = env[-1] if len(env) else 0
    return out


def master(x: np.ndarray) -> np.ndarray:
    # glue compressor on the stereo sum
    lvl = np.sqrt(np.mean(x ** 2, axis=1))
    env = smooth_env(lvl, 0.01, 0.25)
    thr = 0.18
    g = np.where(env > thr, (thr / np.maximum(env, 1e-9)) ** (1 - 1 / 2.0), 1.0)
    x = x * g[:, None]
    # true-peak-ish limiter at -1 dBFS
    peak = np.abs(x).max(axis=1)
    pe = smooth_env(peak, 0.0015, 0.12)
    lim = 0.89
    g = np.where(pe > lim, lim / np.maximum(pe, 1e-9), 1.0)
    x = x * g[:, None]
    return np.clip(x, -0.98, 0.98)


def limit(x: np.ndarray, ceiling: float = 0.84) -> np.ndarray:
    """look-ahead peak limiter: gain from the max over a 4 ms window, smoothed release"""
    from scipy.ndimage import maximum_filter1d, minimum_filter1d
    peak = np.abs(x).max(axis=1)
    win = int(0.004 * SR)
    pk = maximum_filter1d(peak, size=2 * win + 1)
    g = np.minimum(1.0, ceiling / np.maximum(pk, 1e-9))
    g = minimum_filter1d(g, size=2 * win + 1)
    # smooth (release ~80 ms) without letting gain rise above the instantaneous requirement
    a = math.exp(-1 / (0.08 * SR / 48))
    gd = g[: len(g) // 48 * 48].reshape(-1, 48).min(axis=1)
    out = np.empty_like(gd)
    prev = 1.0
    for i, v in enumerate(gd):
        prev = v if v < prev else a * prev + (1 - a) * v
        out[i] = prev
    gs = np.repeat(out, 48)
    gs = np.concatenate([gs, np.full(len(g) - len(gs), gs[-1] if len(gs) else 1.0)])
    gs = np.minimum(gs, g)
    return np.clip(x * gs[:, None], -0.98, 0.98)


def lufs(x: np.ndarray) -> float:
    """ITU-R BS.1770 integrated loudness (approximate K-weighting, gated)."""
    b1, a1 = sg.iirfilter(2, 1500, btype="highpass", ftype="butter", fs=SR)  # shelf approximation
    hp = sg.lfilter(*sg.butter(2, 60, "high", fs=SR), x, axis=0)
    k = hp + 0.58 * sg.lfilter(b1, a1, hp, axis=0)
    blk, hop = int(0.4 * SR), int(0.1 * SR)
    pw = []
    for i in range(0, len(k) - blk, hop):
        pw.append(np.mean(k[i:i + blk] ** 2, axis=0).sum())
    pw = np.array(pw)
    l = -0.691 + 10 * np.log10(np.maximum(pw, 1e-12))
    g1 = pw[l > -70]
    rel = -0.691 + 10 * np.log10(g1.mean()) - 10
    g2 = pw[(l > -70) & (l > rel)]
    return -0.691 + 10 * np.log10(g2.mean())


def write(path: pathlib.Path, x: np.ndarray) -> None:
    wavfile.write(path, SR, x.astype(np.float32))


def main() -> None:
    out = B / "audio"
    out.mkdir(parents=True, exist_ok=True)
    stems = compose()
    mix_levels = {"pad": 0.55, "arp": 0.42, "bass": 0.34, "drums": 0.48, "bells": 0.35, "keys": 0.55, "drone": 0.55}
    music = sum(stems[k] * v for k, v in mix_levels.items())
    music = filt(music, "high", 36)
    wet = reverb(filt(music, "high", 180) * 0.32)
    music = music + wet
    sfx_buf = build_sfx()
    sfx_buf = sfx_buf + reverb(sfx_buf * 0.22, IR_SHORT)
    vo = build_vo()
    vo_st = np.stack([vo, vo], axis=1)
    vo_st = vo_st + reverb(vo_st * 0.05, IR_SHORT)
    # duck the score under narration
    venv = smooth_env(np.abs(vo), 0.08, 0.6)
    duck = 1 - 0.62 * np.clip(venv / 0.05, 0, 1)
    music_d = music * duck[:, None]
    # balance: speech on top, score beneath, effects in between
    sfx_d = sfx_buf * (1 - 0.3 * np.clip(venv / 0.05, 0, 1))[:, None]
    mix = vo_st * 1.0 + music_d * 0.52 + sfx_d * 0.55
    # head/tail
    fade = np.ones(N)
    fl = int(1.8 * SR)
    fade[-fl:] = np.linspace(1, 0, fl) ** 1.6
    mix *= fade[:, None]
    mix = master(mix)
    target = -16.0
    for _ in range(4):  # gain toward the target, then limit; repeat until it converges
        L = lufs(mix)
        if abs(L - target) < 0.3:
            break
        mix = limit(mix * 10 ** ((target - L) / 20))
    print(f"loudness {lufs(mix):.1f} LUFS (target {target}), peak {20 * math.log10(np.abs(mix).max()):.2f} dBFS")
    print(f"stems rms: music {rms_db(music):.1f} dB, sfx {rms_db(sfx_buf):.1f} dB, vo {rms_db(vo):.1f} dB")
    write(out / "music.wav", music)
    write(out / "sfx.wav", sfx_buf)
    write(out / "vo.wav", vo_st)
    write(out / "mix.wav", mix)
    (ROOT / "out").mkdir(exist_ok=True)
    subprocess.run([FF, "-y", "-loglevel", "error", "-i", str(out / "mix.wav"), "-c:a", "libmp3lame", "-b:a", "192k", str(ROOT / "src" / "soundtrack.mp3")], check=True)
    subprocess.run([FF, "-y", "-loglevel", "error", "-i", str(out / "mix.wav"), "-c:a", "aac", "-b:a", "256k", str(ROOT / "out" / "soundtrack.m4a")], check=True)
    print("wrote", ROOT / "src" / "soundtrack.mp3")


if __name__ == "__main__":
    main()
