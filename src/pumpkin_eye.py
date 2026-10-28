#!/usr/bin/env python3
"""Cyclops pumpkin eye: a normal eye that transforms into a dino/reptile eye.

    python3 pumpkin_eye.py --mode normal    # animation 1: normal eye looking around, blinking (loops)
    python3 pumpkin_eye.py --mode scary     # animation 2: transforms into the dino eye once, then loops it
    python3 pumpkin_eye.py                  # both chained: normal -> transform -> scary -> back (loops)
    python3 pumpkin_eye.py --preview eye.gif   # render to a GIF instead of the display

All frames are drawn procedurally with numpy, pre-rendered, then streamed.
"""
import argparse
import math
import os
import time

import cv2
import numpy as np

W = H = 240
C = 119.5
FPS = 20
TIMELINE_FPS = 10   # the phase timelines below count frames at this rate; in-betweens fill up to FPS
Y, X = np.mgrid[0:H, 0:W].astype(np.float32)
R_SCREEN = np.hypot(X - C, Y - C)


def lerp(a, b, t):
    return a + (b - a) * t


def ease(t):
    t = min(max(t, 0.0), 1.0)
    return t * t * (3 - 2 * t)


def edge(d, width=1.5):
    """Anti-aliased coverage: 1 where signed distance d < 0, 0 where d > 0."""
    return np.clip(0.5 - d / width, 0.0, 1.0)


def blend(img, color, alpha):
    a = alpha[..., None]
    return img * (1 - a) + np.asarray(color, np.float32) * a


# ---------------------------------------------------------------- textures

def _ring_noise(n, fmin, fmax, seed):
    rng = np.random.default_rng(seed)
    x = np.linspace(0, 2 * np.pi, n, endpoint=False)
    s = np.zeros(n)
    for f in range(fmin, fmax):
        s += rng.normal() / math.sqrt(f) * np.sin(f * x + rng.uniform(0, 2 * np.pi))
    return (s / np.abs(s).max()).astype(np.float32)


STREAKS = _ring_noise(1024, 8, 140, 1)      # iris fibres
CRACKS = _ring_noise(1024, 40, 260, 2)      # sharper reptile fibres


def _make_veins(seed=3):
    """Per-pixel 'depth' of blood vessels growing in from the rim (0=rim, ~0.7=deep)."""
    rng = np.random.default_rng(seed)
    segs = []

    def grow(x, y, a, d, steps, th):
        for i in range(steps):
            a += rng.normal(0, 0.35)
            nx, ny = x + 3.5 * math.cos(a), y + 3.5 * math.sin(a)
            segs.append((d, (x, y), (nx, ny), th))
            x, y, d, th = nx, ny, d + 1 / 40, max(1.0, th * 0.96)
            if rng.random() < 0.08 and steps - i > 6:
                grow(x, y, a + rng.choice([-0.8, 0.8]), d, (steps - i) // 2, max(1.0, th * 0.7))

    for k in range(18):
        a0 = 2 * math.pi * k / 18 + rng.uniform(-0.2, 0.2)
        grow(C + 124 * math.cos(a0), C + 124 * math.sin(a0),
             a0 + math.pi + rng.normal(0, 0.3), 0.0, int(rng.integers(14, 28)), 2.6)

    depth = np.full((H, W), 9.0, np.float32)
    for d, p0, p1, th in sorted(segs, key=lambda s: -s[0]):
        cv2.line(depth, (int(p0[0]), int(p0[1])), (int(p1[0]), int(p1[1])),
                 float(d), thickness=int(round(th)))
    return depth


VEINS = _make_veins()

# iris colour stops: (radius fraction, RGB)
NORMAL_IRIS = ([0.0, 0.35, 0.55, 0.85, 1.0],
               [(120, 80, 30), (175, 135, 60), (70, 140, 165), (40, 95, 125), (15, 30, 45)])
SCARY_IRIS = ([0.0, 0.3, 0.6, 0.88, 1.0],
              [(255, 250, 150), (255, 215, 40), (240, 120, 10), (140, 25, 0), (20, 0, 0)])


def _gradient(rn, stops):
    pos, cols = stops
    cols = np.asarray(cols, np.float32)
    return np.dstack([np.interp(rn, pos, cols[:, c]) for c in range(3)]).astype(np.float32)


# ---------------------------------------------------------------- one frame

def render(s):
    """Render one frame from a state dict. Returns HxWx3 uint8 RGB."""
    t = s["t"]                                   # 0 = normal eye, 1 = full monster
    ix, iy = C + s["look"][0], C + s["look"][1]

    # sclera with spherical shading
    shade = 1 - 0.45 * np.clip(R_SCREEN / 122, 0, 1) ** 3
    sclera = lerp(np.array([240, 235, 225], np.float32), np.array([165, 30, 15], np.float32), t)
    img = sclera * shade[..., None]

    # blood vessels creeping in
    reveal = lerp(0.18, 0.75, t) + s.get("vein_boost", 0)
    vmask = cv2.GaussianBlur(np.clip((reveal - VEINS) * 10, 0, 1), (3, 3), 0)
    img = blend(img, lerp(np.array([200, 70, 70]), np.array([70, 0, 0]), t), vmask * lerp(0.5, 0.95, t))

    dx, dy = X - ix, Y - iy
    ri = np.hypot(dx, dy)
    RI = lerp(52, 74, t) * s.get("iris_scale", 1.0)

    # hellish glow around the monster iris
    if t > 0:
        glow = np.exp(-np.clip(ri - RI, 0, None) / 14) * (ri > RI) * 0.6 * t * s.get("glow", 1.0)
        img = img + glow[..., None] * np.array([255, 110, 0], np.float32)

    # iris
    rn = np.clip(ri / RI, 0, 1)
    idx = (((np.arctan2(dy, dx) + np.pi) / (2 * np.pi)) * 1023).astype(np.int32)
    iris = lerp(_gradient(rn, NORMAL_IRIS), _gradient(rn, SCARY_IRIS), t)
    fib = STREAKS[idx] * lerp(0.25, 0.35, t) + CRACKS[idx] * lerp(0.05, 0.3, t)
    iris *= (1 + fib * np.clip(rn * 3 - 0.3, 0, 1))[..., None]
    iris *= (1 - 0.75 * np.clip((rn - 0.84) / 0.16, 0, 1))[..., None]   # dark limbal ring
    img = blend(img, iris, edge(ri - RI))
    img = np.where((edge(ri - RI) > 0)[..., None], np.minimum(img, 255), img)

    # pupil: round for the human eye, pointed slit for the reptile
    round_a = edge(ri - s["pupil_r"])
    a, b = s["slit"]
    w = a * (1 - (dy / b) ** 2)
    slit_a = edge(np.abs(dx) - w)
    pupil = lerp(round_a, slit_a, s["shape"])
    img = blend(img, (5, 0, 0), pupil)

    # specular highlights (dimmer on the monster)
    hl = lerp(0.9, 0.55, t)
    img = blend(img, (255, 255, 255), edge(np.hypot(X - (ix - 20), Y - (iy - 24)) - 9, 3) * hl)
    img = blend(img, (255, 255, 255), edge(np.hypot(X - (ix + 15), Y - (iy + 17)) - 4, 2) * hl * 0.7)

    # eyelids (pumpkin-orange skin)
    close = max(s["blink"], s["squint"])
    xn = (X - C) / 120
    y_up = lerp(-12, C + 18, close) + lerp(60, 8, close) * xn ** 2 + s["tilt"] * (X - C) * (1 - s["blink"])
    y_lo = lerp(C + 128, C + 18, s["blink"]) - lerp(55, 8, s["blink"]) * xn ** 2
    shadow = 1 - 0.45 * np.exp(-np.clip(Y - y_up, 0, None) / 10)
    img *= shadow[..., None]
    lid_a = np.maximum(edge(Y - y_up), edge(y_lo - Y))
    lid_col = lerp(np.array([235, 120, 20], np.float32), np.array([150, 45, 0], np.float32), t)
    lid = lid_col * (1 - 0.5 * np.clip(R_SCREEN / 122, 0, 1) ** 2)[..., None]
    lash = np.maximum(np.exp(-np.abs(Y - y_up) / 2.5), np.exp(-np.abs(Y - y_lo) / 2.5))
    lid = lid * (1 - 0.85 * lash)[..., None]
    img = blend(img, lid, lid_a)

    # red flash, socket vignette, shake
    if s.get("flash", 0) > 0:
        img = blend(img, (255, 0, 0), np.full((H, W), s["flash"], np.float32))
    img *= np.clip((121 - R_SCREEN) / 10, 0, 1)[..., None] * 0.35 + np.clip((121 - R_SCREEN) / 3, 0, 1)[..., None] * 0.65
    out = np.clip(img, 0, 255).astype(np.uint8)
    sx, sy = s.get("shake", (0, 0))
    if sx or sy:
        out = np.roll(out, (int(sy), int(sx)), axis=(0, 1))
    return out


# ---------------------------------------------------------------- timelines

def _track(keys, n, move=3):
    """Saccade-style motion: jump to each (frame, x, y) key over `move` frames, then hold."""
    out, prev = [], (0.0, 0.0)
    ki = 0
    for f in range(n):
        while ki + 1 < len(keys) and keys[ki + 1][0] <= f:
            prev = keys[ki][1:]
            ki += 1
        k = keys[ki]
        p = ease((f - k[0]) / move) if f >= k[0] else 0
        out.append((lerp(prev[0], k[1], p), lerp(prev[1], k[2], p)))
    return out


def _blinks(starts, n, curve=(0.6, 1.0, 0.7, 0.25)):
    out = [0.0] * n
    for s0 in starts:
        for i, v in enumerate(curve):
            if s0 + i < n:
                out[s0 + i] = max(out[s0 + i], v)
    return out


def _base(**kw):
    s = dict(t=0.0, look=(0, 0), pupil_r=21, slit=(6, 58), shape=0.0,
             blink=0.0, squint=0.0, tilt=0.0)
    s.update(kw)
    return s


def normal_phase():
    n = 70
    look = _track([(0, 0, 0), (10, -32, 4), (22, 28, -8), (34, 0, 0), (46, 14, 22), (58, -6, -4)], n)
    blink = _blinks([18, 50], n)
    return [_base(look=look[f], blink=blink[f], pupil_r=21 + 2 * math.sin(f / 9)) for f in range(n)]


def transform_phase(rng):
    frames = []
    # 1) something is wrong: twitchy darting, pupil blowing wide, trembling
    for f in range(14):
        p = f / 13
        j = rng.normal(0, 14 * p, 2) if f % 2 else (0, 0)
        frames.append(_base(look=(j[0], j[1]), pupil_r=lerp(21, 34, ease(p)),
                            shake=tuple(rng.integers(-3, 4, 2) * p), vein_boost=0.3 * p,
                            blink=0.35 if f in (9, 10) else 0.0))
    # 2) lids slam shut, red flash
    for f, (bl, fl) in enumerate([(0.7, 0.3), (1.0, 0.6), (1.0, 0.4), (1.0, 0.2), (0.8, 0.0)]):
        frames.append(_base(t=0.5, blink=bl, flash=fl, pupil_r=34, shake=tuple(rng.integers(-5, 6, 2))))
    # 3) reopens as the beast: slit closes in, veins and glow spread
    n = 22
    for f in range(n):
        p = ease(f / (n - 1))
        frames.append(_base(t=lerp(0.5, 1.0, p), pupil_r=34, shape=ease(f / 6),
                            slit=(lerp(30, 6, p), lerp(34, 58, p)),
                            blink=max(0.0, 0.6 - f * 0.12), squint=0.28 * p, tilt=0.12 * p,
                            shake=tuple(rng.integers(-2, 3, 2) * (1 - p)), glow=lerp(1.6, 1.0, p)))
    return frames


def scary_phase(rng):
    n = 80
    look = _track([(0, 0, 0), (12, -36, 2), (28, 36, -4), (44, 0, 6)], n, move=6)
    blink = _blinks([70], n, curve=(0.5, 1.0, 1.0, 0.6, 0.3))
    frames = []
    for f in range(n):
        # at ~50 it "notices you": slit dilates, iris swells, a little lunge shake
        notice = ease((f - 50) / 4) * (1 - ease((f - 64) / 6))
        a = lerp(6 + 1.5 * math.sin(f / 4), 15, notice)
        frames.append(_base(t=1.0, look=look[f], shape=1.0, slit=(a, 58),
                            iris_scale=1 + 0.08 * notice, glow=1 + 0.8 * notice + 0.2 * math.sin(f / 3),
                            squint=lerp(0.28, 0.12, notice), tilt=0.12, blink=blink[f],
                            shake=tuple(rng.integers(-2, 3, 2)) if 50 <= f < 56 else (0, 0)))
    return frames


def revert_phase():
    frames = []
    for f, bl in enumerate([0.5, 0.9, 1.0, 1.0, 1.0, 1.0, 0.8, 0.5, 0.2, 0.0]):
        t = 1.0 if f < 3 else 0.0
        frames.append(_base(t=t, blink=bl, shape=1.0 if t else 0.0, squint=0.28 if t else 0.0,
                            tilt=0.12 if t else 0.0))
    return frames


PHASES = {
    "normal": lambda rng: normal_phase(),
    "transform": transform_phase,
    "scary": scary_phase,
    "revert": lambda rng: revert_phase(),
}

ANIMATIONS = {
    # name: (phases played once, phases that loop)
    "normal": ([], ["normal"]),
    "scary": (["transform"], ["scary"]),
    "cycle": ([], ["normal", "transform", "scary", "revert"]),
}


LOOPING = {"normal", "scary"}


def _resample(states, k, loop):
    """Add k-1 interpolated states after each one, so a TIMELINE_FPS timeline plays at FPS.

    Shake is held rather than interpolated, so the trembling stays jerky.
    """
    def mix(a, b, p):
        if isinstance(a, tuple):
            return tuple(lerp(x, y, p) for x, y in zip(a, b))
        return lerp(a, b, p)

    out = []
    for i, a in enumerate(states):
        b = states[(i + 1) % len(states)] if loop or i + 1 < len(states) else a
        for j in range(k):
            out.append({key: v if key == "shake" else mix(v, b.get(key, v), j / k)
                        for key, v in a.items()})
    return out


def build_phase(name):
    """Render one phase to a list of RGB arrays."""
    rng = np.random.default_rng(13)
    states = _resample(PHASES[name](rng), FPS // TIMELINE_FPS, name in LOOPING)
    return [render(s) for s in states]


def load_phase(name, rotate=90):
    """RGB565 frames for a phase, rotated anticlockwise by `rotate` degrees.

    Cached next to this script and re-rendered automatically if the script changes.
    """
    from gc9a01 import to_rgb565
    cache = os.path.join(os.path.dirname(os.path.abspath(__file__)), f".eye_cache_{name}.npy")
    if os.path.exists(cache) and os.path.getmtime(cache) > os.path.getmtime(__file__):
        frames = np.load(cache)
    else:
        print(f"rendering '{name}' frames (first run only)...")
        frames = np.array([np.frombuffer(to_rgb565(f), np.uint8) for f in build_phase(name)])
        np.save(cache, frames)
    k = rotate // 90
    return [np.ascontiguousarray(np.rot90(f.reshape(H, W, 2), k)).ravel() for f in frames]


class FrameClock:
    """Plays frames on the display at FPS, dropping frames when the display falls behind.

    Big frames (most of the scary phase) take longer than 1/FPS to send, so show()
    returns how many frames to advance; that keeps the animation at real-time speed.
    """

    def __init__(self, lcd):
        self.lcd, self.period, self.due = lcd, 1 / FPS, None

    def show(self, buf):
        now = time.monotonic()
        if self.due is None or now - self.due > 1.0:   # first frame, or resumed after a pause
            self.due = now
        self.lcd.show_raw(buf)
        self.due += self.period
        frames = 1
        while time.monotonic() > self.due + self.period:   # a whole slot behind: drop a frame
            self.due += self.period
            frames += 1
        remaining = self.due - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        return frames


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=list(ANIMATIONS), default="cycle")
    ap.add_argument("--preview", help="write an animated GIF instead of using the display")
    ap.add_argument("--rotate", type=int, default=90, choices=[0, 90, 180, 270],
                    help="rotate the image anticlockwise by this many degrees")
    args = ap.parse_args()
    once_names, loop_names = ANIMATIONS[args.mode]

    if args.preview:
        from PIL import Image
        imgs = [Image.fromarray(f) for n in once_names + loop_names for f in build_phase(n)]
        imgs[0].save(args.preview, save_all=True, append_images=imgs[1:],
                     duration=1000 // FPS, loop=0)
        print("saved", args.preview)
        return

    from gc9a01 import GC9A01
    intro = [f for n in once_names for f in load_phase(n, args.rotate)]
    loop = [f for n in loop_names for f in load_phase(n, args.rotate)]
    print(f"{len(intro)} intro + {len(loop)} loop frames")
    lcd = GC9A01()
    clock = FrameClock(lcd)

    def play(bufs):
        i = 0
        while i < len(bufs):
            i += clock.show(bufs[i])

    try:
        play(intro)
        while True:
            play(loop)
    except KeyboardInterrupt:
        pass
    finally:
        lcd.clear()
        lcd.close()


if __name__ == "__main__":
    main()
