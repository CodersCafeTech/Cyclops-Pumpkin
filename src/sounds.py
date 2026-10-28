"""Background sound playback through the RDK X5 3.5 mm jack (ES8326, ALSA card 0).

Each play() runs a small shell loop in its own process group, so it keeps going
while the Python main loop is busy pushing display frames, and stop() kills it.
"""
import os
import shlex
import signal
import subprocess

SOUND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sounds")


def _player_cmd():
    # PulseAudio if a desktop session is running, otherwise straight to the jack via ALSA
    if subprocess.run(["pactl", "info"], capture_output=True).returncode == 0:
        return "paplay"
    return "aplay -q -D plughw:0,0"


def find(name):
    """Path of sounds/<name>.wav/.ogg/.flac, or None."""
    for ext in (".wav", ".ogg", ".flac"):
        p = os.path.join(SOUND_DIR, name + ext)
        if os.path.exists(p):
            return p
    return None


def duration(path):
    """Length of a sound file in seconds."""
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", path], capture_output=True, text=True)
    return float(out.stdout.strip() or 0)


class SoundPlayer:
    def __init__(self):
        self.cmd = _player_cmd()
        self.proc = None

    def play(self, once=(), loop=None, delay=0.0):
        """After `delay` seconds play the `once` sounds in order, then repeat `loop` until stopped."""
        self.stop()
        parts = [f"sleep {delay}"] if delay > 0 else []
        parts += [f"{self.cmd} {shlex.quote(p)}" for p in once if p]
        if loop:
            parts.append(f"while :; do {self.cmd} {shlex.quote(loop)}; done")
        if len(parts) > (delay > 0):
            self.proc = subprocess.Popen(["sh", "-c", "; ".join(parts)], start_new_session=True,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            self.proc.wait()
        self.proc = None
