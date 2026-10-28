#!/usr/bin/env python3
"""Cyclops pumpkin: USB camera + BPU person/face detection drives the eye, relay and sounds.

    python3 pumpkin.py                 # auto-pick the USB camera
    python3 pumpkin.py --camera 0 --debug

Nobody         -> normal eye loops, relay OFF, silent (spooky ambience with --ambient).
Person far     -> ghostly voice whispers "I see you..." (at most once per --whisper-every seconds).
Face up close  -> ghostly laugh with yellow LEDs pulsing to it, eye transforms into the
                  dino eye, relay ON, then growls.
--hold seconds without a close face -> eye blinks back to normal, relay OFF, sound stops.

Sounds come from sounds/iseeyou, laugh, growl, ambience (.wav/.ogg/.flac) out of the 3.5 mm jack.

Detection runs on the RDK X5 BPU with the multitask body/head/face/hand
model that ships with TROS (mono2d_body_detection).
"""
import argparse
import glob
import multiprocessing
import sys
import time

import cv2
import numpy as np
from hobot_dnn import pyeasy_dnn as dnn

from gc9a01 import GC9A01
from pumpkin_eye import FrameClock, load_phase
from relay import Relay
from sounds import SoundPlayer, duration, find

mp = multiprocessing.get_context("spawn")   # fork is unsafe once OpenCV/BPU runtime threads exist

MODEL = "/opt/tros/humble/lib/mono2d_body_detection/config/multitask_body_head_face_hand_kps_960x544.hbm"
MODEL_W, MODEL_H = 960, 544
# float outputs of the multitask model; rows are [x1, y1, x2, y2, score, cls], padded with -1
BODY_OUTPUT, HEAD_OUTPUT, FACE_OUTPUT = 1, 3, 5


def to_nv12(bgr):
    """Letterbox a BGR frame into 960x544 and convert to NV12 for the BPU."""
    h, w = bgr.shape[:2]
    scale = min(MODEL_W / w, MODEL_H / h)
    nw, nh = int(w * scale) & ~1, int(h * scale) & ~1
    canvas = np.zeros((MODEL_H, MODEL_W, 3), np.uint8)
    canvas[:nh, :nw] = cv2.resize(bgr, (nw, nh))
    i420 = cv2.cvtColor(canvas, cv2.COLOR_BGR2YUV_I420).reshape(-1)
    n = MODEL_W * MODEL_H
    nv12 = np.empty(n * 3 // 2, np.uint8)
    nv12[:n] = i420[:n]
    nv12[n::2] = i420[n:n + n // 4]
    nv12[n + 1::2] = i420[n + n // 4:]
    return nv12, scale


def open_camera(spec):
    """Open the camera given as an index/path, or the first /dev/video* that delivers frames."""
    candidates = [spec] if spec is not None else sorted(glob.glob("/dev/video*"))
    for c in candidates:
        cap = cv2.VideoCapture(int(c) if str(c).isdigit() else c, cv2.CAP_V4L2)
        if not cap.isOpened():
            continue
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        ok, _ = cap.read()
        if ok:
            print(f"camera: {c}")
            return cap
        cap.release()
    return None


class PersonWatcher(mp.Process):
    """Grabs camera frames, runs BPU detection, records when a person / a close face was last seen.

    Runs as a separate process: spidev holds the GIL while pushing display frames,
    which would starve a thread.
    """

    def __init__(self, camera, threshold, near_face, debug):
        super().__init__(daemon=True)
        self.camera, self.threshold, self.near_face, self.debug = camera, threshold, near_face, debug
        # time.monotonic() is system-wide, so these timestamps are shareable between processes
        self._person_seen = mp.Value("d", -1e9)
        self._near_seen = mp.Value("d", -1e9)
        self.model = None
        self.person_streak = self.near_streak = 0

    @property
    def person_seen(self):
        return self._person_seen.value

    @property
    def near_seen(self):
        return self._near_seen.value

    def detect(self, bgr):
        """Return (number of people-ish detections, face heights in camera pixels)."""
        nv12, scale = to_nv12(bgr)
        outs = self.model.forward(nv12)

        def boxes(idx):
            rows = outs[idx].buffer.reshape(-1, 6)
            return rows[rows[:, 4] >= self.threshold].copy()

        faces = boxes(FACE_OUTPUT)
        people = len(boxes(BODY_OUTPUT)) + len(boxes(HEAD_OUTPUT)) + len(faces)
        return people, (faces[:, 3] - faces[:, 1]) / scale

    def run(self):
        sys.stdout.reconfigure(line_buffering=True)   # spawned child doesn't inherit -u
        try:
            self._watch()
        except KeyboardInterrupt:
            pass

    def _watch(self):
        self.model = dnn.load(MODEL)[0]
        cap = None
        while True:
            if cap is None:
                cap = open_camera(self.camera)
                if cap is None:
                    print("no camera found, retrying in 3 s...")
                    time.sleep(3)
                    continue
            ok, frame = cap.read()
            if not ok:
                print("camera read failed, reopening...")
                cap.release()
                cap = None
                time.sleep(1)
                continue
            people, face_heights = self.detect(frame)
            near = bool(len(face_heights)) and face_heights.max() >= self.near_face
            # need 2 detections in a row so a single false positive doesn't trigger anything
            self.person_streak = self.person_streak + 1 if people else 0
            self.near_streak = self.near_streak + 1 if near else 0
            now = time.monotonic()
            if self.person_streak >= 2:
                self._person_seen.value = now
            if self.near_streak >= 2:
                self._near_seen.value = now
            if self.debug and people:
                print(f"people: {people}  face heights {face_heights.astype(int)}px  near={near}")


class LedProcess(mp.Process):
    """Drives the WS2812B strip (SPI1) with the yellow laugh lights, in sync with laugh.wav."""

    def __init__(self, laugh_path, leds, offset):
        super().__init__(daemon=True)
        self.laugh_path, self.leds, self.offset = laugh_path, leds, offset
        self._start = mp.Value("d", -1e9)   # time.monotonic() when the laugh started
        self._stop = mp.Event()

    def trigger(self):
        self._start.value = time.monotonic() + self.offset   # offset = audio output latency

    def stop(self):
        self._stop.set()
        self.join(timeout=2)

    def run(self):
        sys.stdout.reconfigure(line_buffering=True)
        from ws2812 import LaughLights, Strip, loudness_envelope
        try:
            strip = Strip(n=self.leds)
        except OSError as e:
            print(f"LED strip disabled (SPI1 not available: {e})")
            return
        lights = LaughLights(strip, loudness_envelope(self.laugh_path))
        lit = False
        try:
            while not self._stop.is_set():
                t = time.monotonic() - self._start.value
                if 0 <= t < lights.duration:
                    strip.show(lights.frame(t))
                    lit = True
                elif lit:
                    strip.off()
                    lit = False
                time.sleep(1 / 50)
        except KeyboardInterrupt:
            pass
        finally:
            strip.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--camera", help="camera index or /dev/videoN (default: auto)")
    ap.add_argument("--threshold", type=float, default=0.7, help="detection score threshold")
    ap.add_argument("--near-face", type=int, default=80,
                    help="face height in pixels (of 480) that counts as 'up close' -> scare")
    ap.add_argument("--whisper-every", type=float, default=20.0,
                    help="minimum seconds between 'I see you' whispers")
    ap.add_argument("--hold", type=float, default=4.0,
                    help="seconds without a close face before the eye goes back to normal")
    ap.add_argument("--rotate", type=int, default=90, choices=[0, 90, 180, 270])
    ap.add_argument("--ambient", action="store_true",
                    help="play the spooky wind/drone/heartbeat ambience while waiting (default: silent)")
    ap.add_argument("--mute", action="store_true", help="no sounds")
    ap.add_argument("--leds", type=int, default=5, help="number of WS2812B LEDs (0 = no strip)")
    ap.add_argument("--led-offset", type=float, default=0.15,
                    help="seconds to delay the lights so they line up with the speaker")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    phases = {n: load_phase(n, args.rotate) for n in ("normal", "transform", "scary", "revert")}
    relay = Relay()
    relay.off()
    watcher = PersonWatcher(args.camera, args.threshold, args.near_face, args.debug)
    watcher.start()
    lcd = GC9A01()
    clock = FrameClock(lcd)
    audio = SoundPlayer()   # ambience / growl
    voice = SoundPlayer()   # "I see you", plays over the ambience
    growl, ambience = find("growl"), find("ambience")
    iseeyou, laugh = find("iseeyou"), find("laugh")
    laugh_len = duration(laugh) if laugh else 0.0
    lights = None
    if laugh and args.leds > 0:
        lights = LedProcess(laugh, args.leds, args.led_offset)
        lights.start()
    last_whisper = -1e9

    def normal_sound():
        if args.ambient and not args.mute:
            audio.play(loop=ambience)
        else:
            audio.stop()

    def scary_now():
        return time.monotonic() - watcher.near_seen < args.hold

    def person_now():
        return time.monotonic() - watcher.person_seen < 0.5

    state, i = "normal", 0
    normal_sound()
    print("running - Ctrl+C to stop")
    try:
        while True:
            frames = phases[state]
            i += clock.show(frames[i])
            if state == "normal" and scary_now():
                print("face up close! -> scary")
                relay.on()
                if not args.mute:
                    # ghostly laugh right away while the eye transforms; growl only once it's done
                    voice.play(once=[laugh])
                    if lights:
                        lights.trigger()
                    last_whisper = time.monotonic()   # don't cut the laugh off with "I see you"
                    audio.play(loop=growl, delay=laugh_len)
                state, i = "transform", 0
            elif state == "scary" and not scary_now():
                print("scare over -> normal")
                state, i = "revert", 0
                audio.stop()
            elif i >= len(frames):
                if state == "transform":
                    state = "scary"
                elif state == "revert":
                    relay.off()
                    normal_sound()
                    state = "normal"
                i = 0
            # the whisper doesn't change the eye, so it's kept out of the frame/state chain above
            if (state == "normal" and person_now() and iseeyou and not args.mute
                    and time.monotonic() - last_whisper > args.whisper_every):
                print("someone out there -> 'I see you...'")
                voice.play(once=[iseeyou])
                last_whisper = time.monotonic()
    except KeyboardInterrupt:
        pass
    finally:
        audio.stop()
        voice.stop()
        if lights:
            lights.stop()
        relay.off()
        lcd.clear()
        lcd.close()


if __name__ == "__main__":
    main()
