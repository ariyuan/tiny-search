#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""iometer.py — a rate meter over cumulative byte counters (sampler injected).

Generic infrastructure, not HTTP-specific: give it a sampler returning this
process's cumulative (read_bytes, write_bytes) and it derives MB/s once a second.
The counters are cumulative, so a rate needs two samples: the first one only
records the baseline, every later tick divides the byte delta by the real elapsed
time. Rates clamp at 0 — a counter that ever went backwards (it should not) must
never print a negative speed. One lock guards the shared state; snapshot() never
waits on a tick.

The sampler is injected rather than imported so the web facade can bind it to its
own module namespace (see web_ui.IoMeter — that is the seam tests patch). The
default sampler is this process's disk I/O (platform.win32.process_io_bytes).

Depends on: mft.platform.win32 (default sampler only).
"""

import threading
import time

from mft.platform.win32 import process_io_bytes as _default_sampler


class IoMeter:
    """Samples cumulative I/O counters once a second and derives MB/s rates."""

    def __init__(self, interval: float = 1.0, sampler=None):
        self.interval = interval
        self._sampler = sampler or _default_sampler
        self._lock = threading.Lock()
        self._prev = None                    # (perf_counter, read_bytes, write_bytes)
        self._last = {"read_mb_s": 0.0, "write_mb_s": 0.0,
                      "read_mb": 0.0, "write_mb": 0.0}
        self._available = None               # None = no sample taken yet
        self._stop = threading.Event()
        self._thread = None

    def sample(self) -> None:
        now = time.perf_counter()
        cur = self._sampler()
        if cur is None:
            with self._lock:
                self._available = False
            return
        with self._lock:
            self._available = True
            prev = self._prev
            self._prev = (now, cur[0], cur[1])
            # Session totals are absolute values: record them on every tick, baseline
            # included, so the tooltip never shows 0 for a process that already read data.
            self._last["read_mb"] = cur[0] / 1048576.0
            self._last["write_mb"] = cur[1] / 1048576.0
            if prev is None or now <= prev[0]:
                return                          # baseline tick (or a stalled clock)
            self._last["read_mb_s"] = max(0.0, (cur[0] - prev[1]) / 1048576.0 / (now - prev[0]))
            self._last["write_mb_s"] = max(0.0, (cur[1] - prev[2]) / 1048576.0 / (now - prev[0]))

    def snapshot(self) -> dict:
        with self._lock:
            out = dict(self._last)
            out["available"] = self._available
        return out

    def start(self) -> None:
        if self._thread is not None:
            return
        self.sample()                          # baseline now: first rate lands one interval later
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.sample()
            except Exception:
                pass          # a metering glitch must never take the server down

    def stop(self) -> None:
        self._stop.set()
