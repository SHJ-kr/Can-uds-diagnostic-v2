"""Virtual CAN ID arbitration with model/wall-time accounting.

Only frames released at a bus boundary compete. No artificial arbitration
window is added. Background releases are independent of transmission completion;
each source retains one pending frame and counts later releases as coalesced.
These measurements describe a software model, not physical CAN utilization.
"""
import random
import threading
import time

import can

from virtual_ecu import CAN_BITRATE, frame_tx_time, precise_wait


DEFAULT_BACKGROUND_NODES = [
    ("EngineECU", 0x100, (0.008, 0.012)),
    ("BodyECU", 0x300, (0.018, 0.022)),
    ("InfotainmentECU", 0x600, (0.045, 0.055)),
]


class _PendingFrame:
    def __init__(self, message, enqueued_at=None):
        self.message = message
        self.enqueued_at = time.perf_counter() if enqueued_at is None else enqueued_at
        self.started_at = None
        self.sent_at = None
        self.sent = False
        self.cancelled = False
        self.error = None


class ArbitratedBus:
    def __init__(self, channel, bitrate=CAN_BITRATE, seed=42):
        if bitrate <= 0:
            raise ValueError("bitrate must be positive")
        self.channel = channel
        self.bitrate = bitrate
        self._tx_bus = can.interface.Bus(channel=channel, interface="virtual")
        self._cv = threading.Condition()
        self._pending = []
        self._background = []
        self._rng = random.Random(seed)
        self._stopped = False
        self._active = None
        self._transmissions = []
        self._thread = threading.Thread(target=self._arbiter_loop, daemon=True)
        self._thread.start()

    def submit(self, message):
        with self._cv:
            if self._stopped:
                raise RuntimeError("CAN arbiter stopped")
            ticket = _PendingFrame(message)
            self._pending.append(ticket)
            self._cv.notify_all()
            return ticket

    def transmit(self, message, timeout=3.0):
        ticket = self.submit(message)
        deadline = time.perf_counter() + timeout
        with self._cv:
            while not ticket.sent and ticket.error is None and not self._stopped:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    ticket.cancelled = True
                    if ticket in self._pending:
                        self._pending.remove(ticket)
                    raise TimeoutError("CAN transmit queue timeout")
                self._cv.wait(remaining)
            if ticket.error is not None:
                raise RuntimeError("CAN transmission failed") from ticket.error
            if not ticket.sent:
                raise RuntimeError("CAN arbiter stopped before transmission")
        return ticket

    def configure_background(self, presets):
        now = time.perf_counter()
        sources = []
        for name, arb_id, interval in presets:
            lo, hi = interval
            if not (0 <= arb_id <= 0x7FF) or lo <= 0 or hi < lo:
                raise ValueError("background IDs must be 11-bit; periods must satisfy 0 < min <= max")
            sources.append({
                "name": name, "id": arb_id, "interval": (lo, hi),
                "next": now + self._rng.uniform(0, hi), "ticket": None,
                "released": 0, "coalesced": 0,
            })
        with self._cv:
            self._background = sources
            self._cv.notify_all()

    def _release_background(self, now):
        # Under _cv: all due sources participate in the same ID comparison.
        for source in self._background:
            while source["next"] <= now:
                source["released"] += 1
                previous = source["ticket"]
                if previous is None or previous.sent:
                    message = can.Message(
                        arbitration_id=source["id"], data=[0] * 8, is_extended_id=False,
                    )
                    ticket = _PendingFrame(message, source["next"])
                    self._pending.append(ticket)
                    source["ticket"] = ticket
                else:
                    source["coalesced"] += 1
                source["next"] += self._rng.uniform(*source["interval"])

    def _arbiter_loop(self):
        while True:
            with self._cv:
                if self._stopped:
                    return
                now = time.perf_counter()
                self._release_background(now)
                if not self._pending:
                    wake = min((s["next"] for s in self._background), default=now + 0.05)
                    self._cv.wait(max(0.000001, wake - now))
                    continue
                winner = min(self._pending, key=lambda t: (t.message.arbitration_id, t.enqueued_at))
                self._pending.remove(winner)
                winner.started_at = time.perf_counter()
                duration = frame_tx_time(len(winner.message.data), self.bitrate)
                self._active = (winner.started_at, duration)

            try:
                precise_wait(duration)
                if not winner.cancelled:
                    self._tx_bus.send(winner.message)
                finished = time.perf_counter()
                with self._cv:
                    self._transmissions.append((winner.started_at, finished, duration))
                    self._active = None
                    winner.sent_at = finished
                    winner.sent = True
                    self._cv.notify_all()
            except Exception as exc:
                with self._cv:
                    winner.error = exc
                    self._active = None
                    self._stopped = True
                    self._cv.notify_all()
                return
            time.sleep(0)  # Let ECU/client threads consume the delivered frame.

    def background_counts(self):
        with self._cv:
            return {
                "background_releases": sum(s["released"] for s in self._background),
                "background_coalesced": sum(s["coalesced"] for s in self._background),
            }

    def measure_window(self, start, end=None):
        """Clip wire/service intervals to the caller's observation window.

        Wire time includes the existing stuffing approximation and IFS.
        Software service overhead is reported separately. Neither metric is
        physical-bus instrumentation.
        """
        end = time.perf_counter() if end is None else end
        elapsed = end - start
        if elapsed <= 0:
            raise ValueError("measurement window must be positive")
        with self._cv:
            intervals = list(self._transmissions)
            if self._active:
                began, duration = self._active
                intervals.append((began, end, duration))
            released = sum(s["released"] for s in self._background)
            coalesced = sum(s["coalesced"] for s in self._background)
        wire = service = 0.0
        for began, finished, duration in intervals:
            service += max(0.0, min(finished, end) - max(began, start))
            wire += max(0.0, min(began + duration, finished, end) - max(began, start))
        return {
            "observation_s": elapsed,
            "modeled_wire_busy_s": wire,
            "service_busy_s": service,
            "modeled_bus_load_pct": 100 * wire / elapsed,
            "service_overhead_pct": 100 * (service - wire) / elapsed,
            "background_releases": released,
            "background_coalesced": coalesced,
        }

    def stop(self):
        with self._cv:
            self._stopped = True
            self._cv.notify_all()
        self._thread.join(timeout=1.0)
        if self._thread.is_alive():
            raise RuntimeError("CAN arbiter did not stop")
        self._tx_bus.shutdown()


class BackgroundTrafficNode(threading.Thread):
    """Optional independent sender; experiments use the integrated scheduler."""
    def __init__(self, arbiter, arbitration_id, interval_range, name="bg"):
        super().__init__(daemon=True, name=name)
        self.arbiter = arbiter
        self.arbitration_id = arbitration_id
        self.interval_range = interval_range
        self._stop_flag = threading.Event()

    def stop(self):
        self._stop_flag.set()

    def run(self):
        while not self._stop_flag.is_set():
            try:
                self.arbiter.transmit(can.Message(
                    arbitration_id=self.arbitration_id, data=[0] * 8, is_extended_id=False,
                ))
            except (TimeoutError, RuntimeError):
                return
            self._stop_flag.wait(random.uniform(*self.interval_range))


class BackgroundTrafficScheduler:
    def __init__(self, arbiter, presets):
        self.arbiter = arbiter
        self.presets = list(presets)

    def start(self):
        self.arbiter.configure_background(self.presets)

    def stop(self):
        self.arbiter.configure_background([])


def start_background_traffic(arbiter, presets=DEFAULT_BACKGROUND_NODES):
    scheduler = BackgroundTrafficScheduler(arbiter, presets)
    scheduler.start()
    return [scheduler]
