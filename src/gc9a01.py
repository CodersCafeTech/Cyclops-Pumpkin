"""Minimal GC9A01 driver for the Waveshare 1.28" 240x240 round LCD on RDK X5.

Uses /dev/spidev (spidev) for data and Hobot.GPIO for DC / RST / BL.
Runs on SPI2 (MOSI pin 18, SCLK pin 29, CS pin 31 = spidev2.0): its pads are 3.3 V / 9 mA,
while SPI1's are stuck at 1.8 V / 3 mA, which glitched the display over jumper wires.
Init sequence is the one from Waveshare's LCD_1inch28 reference driver.
"""
import time

import numpy as np
import spidev
import Hobot.GPIO as GPIO

WIDTH = 240
HEIGHT = 240
FULL_REFRESH_S = 2.0   # resend the whole frame this often, so a corrupted pixel can't stick
BAND = 16              # changed pixels are sent per band of rows, which skips the round panel's black corners

# (command, data bytes) — Waveshare LCD_1inch28 init sequence
_INIT_SEQ = [
    (0xEF, []), (0xEB, [0x14]), (0xFE, []), (0xEF, []), (0xEB, [0x14]),
    (0x84, [0x40]), (0x85, [0xFF]), (0x86, [0xFF]), (0x87, [0xFF]),
    (0x88, [0x0A]), (0x89, [0x21]), (0x8A, [0x00]), (0x8B, [0x80]),
    (0x8C, [0x01]), (0x8D, [0x01]), (0x8E, [0xFF]), (0x8F, [0xFF]),
    (0xB6, [0x00, 0x20]),
    (0x3A, [0x05]),  # 16-bit RGB565
    (0x90, [0x08, 0x08, 0x08, 0x08]),
    (0xBD, [0x06]), (0xBC, [0x00]),
    (0xFF, [0x60, 0x01, 0x04]),
    (0xC3, [0x13]), (0xC4, [0x13]), (0xC9, [0x22]), (0xBE, [0x11]),
    (0xE1, [0x10, 0x0E]),
    (0xDF, [0x21, 0x0C, 0x02]),
    (0xF0, [0x45, 0x09, 0x08, 0x08, 0x26, 0x2A]),
    (0xF1, [0x43, 0x70, 0x72, 0x36, 0x37, 0x6F]),
    (0xF2, [0x45, 0x09, 0x08, 0x08, 0x26, 0x2A]),
    (0xF3, [0x43, 0x70, 0x72, 0x36, 0x37, 0x6F]),
    (0xED, [0x1B, 0x0B]), (0xAE, [0x77]), (0xCD, [0x63]),
    (0x70, [0x07, 0x07, 0x04, 0x0E, 0x0F, 0x09, 0x07, 0x08, 0x03]),
    (0xE8, [0x34]),
    (0x62, [0x18, 0x0D, 0x71, 0xED, 0x70, 0x70, 0x18, 0x0F, 0x71, 0xEF, 0x70, 0x70]),
    (0x63, [0x18, 0x11, 0x71, 0xF1, 0x70, 0x70, 0x18, 0x13, 0x71, 0xF3, 0x70, 0x70]),
    (0x64, [0x28, 0x29, 0xF1, 0x01, 0xF1, 0x00, 0x07]),
    (0x66, [0x3C, 0x00, 0xCD, 0x67, 0x45, 0x45, 0x10, 0x00, 0x00, 0x00]),
    (0x67, [0x00, 0x3C, 0x00, 0x00, 0x00, 0x01, 0x54, 0x10, 0x32, 0x98]),
    (0x74, [0x10, 0x85, 0x80, 0x00, 0x00, 0x4E, 0x00]),
    (0x98, [0x3E, 0x07]),
    (0x35, []),  # tearing effect line on
    (0x21, []),  # display inversion on (required by this panel)
]

# MADCTL values for 0/90/180/270 degree rotation (BGR bit set)
_MADCTL = {0: 0x08, 90: 0x68, 180: 0xC8, 270: 0xA8}


def to_rgb565(rgb):
    """HxWx3 uint8 RGB array -> big-endian RGB565 bytes."""
    rgb = rgb.astype(np.uint16)
    v = ((rgb[..., 0] & 0xF8) << 8) | ((rgb[..., 1] & 0xFC) << 3) | (rgb[..., 2] >> 3)
    return v.astype(">u2").tobytes()


class GC9A01:
    def __init__(self, bus=2, device=0, dc=22, rst=13, bl=15,
                 speed_hz=16_000_000, rotation=0):
        """Pin numbers are physical 40-pin header numbers (GPIO.BOARD)."""
        self.dc, self.rst, self.bl = dc, rst, bl
        self._last, self._last_full = None, 0.0

        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BOARD)
        GPIO.setup(dc, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(rst, GPIO.OUT, initial=GPIO.HIGH)
        if bl is not None:
            GPIO.setup(bl, GPIO.OUT, initial=GPIO.LOW)

        self.spi = spidev.SpiDev()
        self.spi.open(bus, device)
        self.spi.mode = 0
        self.spi.max_speed_hz = speed_hz

        self.reset()
        for cmd, data in _INIT_SEQ:
            self.command(cmd, data)
        self.set_rotation(rotation)
        self.command(0x11)  # sleep out
        time.sleep(0.12)
        self.command(0x29)  # display on
        time.sleep(0.02)
        self.backlight(True)

    def reset(self):
        GPIO.output(self.rst, GPIO.HIGH)
        time.sleep(0.01)
        GPIO.output(self.rst, GPIO.LOW)
        time.sleep(0.01)
        GPIO.output(self.rst, GPIO.HIGH)
        time.sleep(0.12)

    def command(self, cmd, data=None):
        GPIO.output(self.dc, GPIO.LOW)
        self.spi.writebytes([cmd])
        if data:
            GPIO.output(self.dc, GPIO.HIGH)
            self.spi.writebytes2(bytes(data))

    def backlight(self, on):
        if self.bl is not None:
            GPIO.output(self.bl, GPIO.HIGH if on else GPIO.LOW)

    def set_rotation(self, degrees):
        self.madctl = _MADCTL[degrees]
        self.command(0x36, [self.madctl])

    def set_window(self, x0, y0, x1, y1):
        self.command(0x2A, [x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF])
        self.command(0x2B, [y0 >> 8, y0 & 0xFF, y1 >> 8, y1 & 0xFF])
        self.command(0x2C)

    def show_raw(self, buf):
        """Show a frame of pre-converted RGB565 bytes (115200 bytes).

        Only what changed since the last frame is sent, as one rectangle per BAND rows
        (SPI2 tops out at ~14 full frames/s), plus everything every FULL_REFRESH_S.
        """
        frame = np.frombuffer(buf, np.uint8).reshape(HEIGHT, WIDTH, 2)
        now = time.monotonic()
        if self._last is not None and now - self._last_full < FULL_REFRESH_S:
            changed = np.any(frame != self._last, axis=2)
        else:
            changed = np.ones((HEIGHT, WIDTH), bool)
            self._last_full = now
        self._last = frame.copy()
        # re-assert the mode registers every frame (a few bytes), so a noise hit on the
        # wires that flips colour format/orientation/inversion only lasts one frame
        self.command(0x3A, [0x05])
        self.command(0x36, [self.madctl])
        self.command(0x21)
        for y in range(0, HEIGHT, BAND):
            cols = np.flatnonzero(changed[y:y + BAND].any(0))
            if len(cols):
                x0, x1 = int(cols[0]), int(cols[-1])
                self.set_window(x0, y, x1, min(y + BAND, HEIGHT) - 1)
                GPIO.output(self.dc, GPIO.HIGH)
                self.spi.writebytes2(np.ascontiguousarray(frame[y:y + BAND, x0:x1 + 1]))

    def show(self, image):
        """Display a PIL Image or HxWx3 RGB numpy array (resized if needed)."""
        if not isinstance(image, np.ndarray):
            image = np.asarray(image.convert("RGB").resize((WIDTH, HEIGHT)))
        self.show_raw(to_rgb565(image))

    def clear(self, color=(0, 0, 0)):
        frame = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
        frame[:] = color
        self.show(frame)

    def close(self):
        self.backlight(False)
        self.spi.close()
        GPIO.cleanup()
