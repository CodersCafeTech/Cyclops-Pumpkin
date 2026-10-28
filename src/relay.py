#!/usr/bin/env python3
"""Relay control on RDK X5 header pin 7 (kernel gpio 420), active-low.

Wiring: VCC -> pin 4 (5V), GND -> pin 6, IN -> pin 7. Pin 7 idles high at boot,
so the relay stays off until this script drives it.

    python3 relay.py on
    python3 relay.py off
    python3 relay.py toggle
    python3 relay.py pulse 3      # on for 3 seconds, then off
    python3 relay.py status

From Python:
    from relay import Relay
    Relay().on()

Uses sysfs directly (not Hobot.GPIO) so the relay keeps its state after the
command exits instead of being reset by GPIO.cleanup().
"""
import os
import sys
import time

GPIO_NUM = 420        # header pin 7
ACTIVE_LOW = True     # set True if your relay module switches on when IN is LOW

_SYS = "/sys/class/gpio"


class Relay:
    def __init__(self, gpio=GPIO_NUM, active_low=ACTIVE_LOW):
        self.path = f"{_SYS}/gpio{gpio}"
        self.active_low = active_low
        if not os.path.exists(self.path):
            with open(f"{_SYS}/export", "w") as f:
                f.write(str(gpio))
            for _ in range(50):  # wait for udev to fix permissions
                if os.access(f"{self.path}/direction", os.W_OK):
                    break
                time.sleep(0.02)
        with open(f"{self.path}/direction") as f:
            if f.read().strip() != "out":
                # "high"/"low" sets direction and initial level in one step, so the relay doesn't click
                with open(f"{self.path}/direction", "w") as f2:
                    f2.write("high" if active_low else "low")

    def _write(self, level):
        with open(f"{self.path}/value", "w") as f:
            f.write("1" if level else "0")

    def set(self, on):
        self._write(on != self.active_low)

    def on(self):
        self.set(True)

    def off(self):
        self.set(False)

    def is_on(self):
        with open(f"{self.path}/value") as f:
            return (f.read().strip() == "1") != self.active_low

    def toggle(self):
        self.set(not self.is_on())

    def pulse(self, seconds):
        self.on()
        time.sleep(seconds)
        self.off()


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("on", "off", "toggle", "pulse", "status"):
        sys.exit(__doc__)
    r = Relay()
    cmd = sys.argv[1]
    if cmd == "pulse":
        r.pulse(float(sys.argv[2]) if len(sys.argv) > 2 else 1.0)
    elif cmd != "status":
        getattr(r, cmd)()
    print("relay is", "ON" if r.is_on() else "OFF")


if __name__ == "__main__":
    main()
