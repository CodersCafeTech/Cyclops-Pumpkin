#!/usr/bin/env python3
"""WS2812B LED strip driven from an SPI MOSI pin (RDK X5 SPI1 MOSI = header pin 19).

Linux can't bit-bang the WS2812 timing, so each LED bit is sent as 5 SPI bits at
4 MHz: 0 -> 10000 (0.25 us high), 1 -> 11100 (0.75 us high).
The 0 pulse is kept short on purpose: SPI1's weak 1.8 V / 3 mA pad makes every high
pulse fall slowly, and with the textbook 0.42 us pulse the strip read all 0s as 1s
(white instead of off, random colours).

    python3 ws2812.py test     # yellow chase, then off
    python3 ws2812.py hold 60  # bright yellow on all LEDs for 60 s (wiring check)
    python3 ws2812.py rgb      # solid red, green, blue for 5 s each
    python3 ws2812.py laugh    # play sounds/laugh.wav with the laugh lights
    python3 ws2812.py off
"""
import sys
import time

import numpy as np
import spidev

SPI_HZ = 4_000_000
SPI_BITS = 5   # SPI bits per LED bit
YELLOW = (255, 255, 0)   # pure yellow: red + green both at full = brightest yellow


def _lut():
    lut = np.zeros((256, SPI_BITS), np.uint8)
    for v in range(256):
        bits = 0
        for i in range(7, -1, -1):
            bits = (bits << SPI_BITS) | (0b11100 if (v >> i) & 1 else 0b10000)
        lut[v] = np.frombuffer(bits.to_bytes(SPI_BITS, "big"), np.uint8)
    return lut


_LUT = _lut()
_RESET = bytes(160)   # 160 bytes low = 320 us latch / reset (newer WS2812B need > 280 us)


class Strip:
    def __init__(self, n=5, bus=1, device=0):
        self.n = n
        self.spi = spidev.SpiDev()
        self.spi.open(bus, device)
        self.spi.mode = 0
        self.spi.max_speed_hz = SPI_HZ

    def show(self, colors):
        """colors: n x 3 RGB values 0..255."""
        grb = np.clip(np.asarray(colors, np.float32), 0, 255).astype(np.uint8)[:, [1, 0, 2]]
        self.spi.writebytes2(_RESET + _LUT[grb.ravel()].tobytes() + _RESET)

    def off(self):
        self.show(np.zeros((self.n, 3)))

    def close(self):
        self.off()
        self.spi.close()


def loudness_envelope(path, rate=50):
    """Loudness of a WAV file, `rate` values per second, scaled 0..1."""
    import wave
    with wave.open(path) as w:
        sr, ch = w.getframerate(), w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768
    x = x.reshape(-1, ch).mean(1)
    hop = sr // rate
    n = len(x) // hop
    rms = np.sqrt((x[:n * hop].reshape(n, hop) ** 2).mean(1))
    env = np.clip(rms / (np.percentile(rms, 95) + 1e-9), 0, 1) ** 0.7
    # fast attack, slower release so the light "breathes" with each ha-ha
    out = np.zeros_like(env)
    for i in range(1, n):
        out[i] = env[i] if env[i] > out[i - 1] else out[i - 1] * 0.82 + env[i] * 0.18
    return out


class LaughLights:
    """Yellow glow that follows the laugh: louder -> brighter and more LEDs lit from the centre."""

    def __init__(self, strip, envelope, rate=50, color=YELLOW, seed=0):
        self.strip, self.env, self.rate = strip, envelope, rate
        self.color = np.asarray(color, np.float32)
        n = strip.n
        self.rank = np.abs(np.arange(n) - (n - 1) / 2) / (n / 2)   # 0 at centre .. ~1 at the ends
        self.rng = np.random.default_rng(seed)

    @property
    def duration(self):
        return len(self.env) / self.rate

    def frame(self, t):
        """LED colours `t` seconds into the laugh."""
        i = int(t * self.rate)
        if i < 0 or i >= len(self.env):
            return np.zeros((self.strip.n, 3))
        b = min(1.0, self.env[i] * 1.5) ** 0.6                          # punchy: most of the laugh near full
        spread = np.clip((b * 1.4 - self.rank) * 3, 0, 1)            # centre lights first
        flicker = 0.9 + 0.1 * self.rng.random(self.strip.n)           # light candle-like shimmer
        return self.color[None, :] * (b * spread * flicker)[:, None]


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "test"
    strip = Strip()
    try:
        if cmd == "test":
            for k in range(3 * strip.n):
                c = np.zeros((strip.n, 3))
                c[k % strip.n] = YELLOW
                strip.show(c)
                time.sleep(0.08)
            for b in np.linspace(0, 1, 30).tolist() + np.linspace(1, 0, 30).tolist():
                strip.show(np.tile(np.asarray(YELLOW) * b, (strip.n, 1)))
                time.sleep(0.02)
        elif cmd == "hold":
            # keep re-sending bright yellow so wiring can be checked while it runs
            end = time.monotonic() + float(sys.argv[2] if len(sys.argv) > 2 else 60)
            while time.monotonic() < end:
                strip.show(np.tile(YELLOW, (strip.n, 1)))
                time.sleep(0.05)
        elif cmd == "rgb":
            # solid red, green, blue for 5 s each: checks colour order and data integrity
            for name, color in (("red", (255, 0, 0)), ("green", (0, 255, 0)), ("blue", (0, 0, 255))):
                print(name)
                end = time.monotonic() + 5
                while time.monotonic() < end:
                    strip.show(np.tile(color, (strip.n, 1)))
                    time.sleep(0.05)
            strip.off()
        elif cmd == "laugh":
            import subprocess
            from sounds import find
            lights = LaughLights(strip, loudness_envelope(find("laugh")))
            subprocess.Popen(["paplay", find("laugh")])
            t0 = time.monotonic()
            while (t := time.monotonic() - t0) < lights.duration:
                strip.show(lights.frame(t))
                time.sleep(1 / 50)
    finally:
        strip.close()


if __name__ == "__main__":
    main()
