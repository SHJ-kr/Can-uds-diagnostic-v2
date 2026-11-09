"""Regression tests for latency accounting, request matching and load isolation."""
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "PYthon_tools"))

import can
import batch_log_generator as batch
from bus_load_sweep import summarize
from can_bus_arbiter import ArbitratedBus


def message(data, arb_id=0x7E8):
    return can.Message(arbitration_id=arb_id, data=data, is_extended_id=False)


class Clock:
    def __init__(self):
        self.now = 10.0

    def perf_counter(self):
        return self.now

    def time(self):
        return self.now


class ScriptedBus:
    def __init__(self, clock, responses):
        self.clock = clock
        self.responses = list(responses)

    def recv(self, timeout):
        end = self.clock.now + max(timeout, 1e-9)
        if self.responses and self.responses[0][0] <= end:
            when, frame = self.responses.pop(0)
            self.clock.now = max(self.clock.now, when)
            return frame
        self.clock.now = end
        return None


class QueueDelay:
    def __init__(self, clock, wait=0.02):
        self.clock = clock
        self.wait = wait

    def transmit(self, msg, timeout):
        began = self.clock.now
        if self.wait >= timeout:
            self.clock.now += timeout
            raise TimeoutError()
        self.clock.now += self.wait
        return SimpleNamespace(enqueued_at=began, started_at=self.clock.now)


class ClientTests(unittest.TestCase):
    def run_script(self, responses, deadline=50, timeout=0.3, wait=0.02):
        clock = Clock()
        bus = ScriptedBus(clock, responses)
        client = batch.UdsClient(bus, arbiter=QueueDelay(clock, wait),
                                 deadline_ms=deadline, hard_timeout=timeout)
        with patch.object(batch, "time", clock):
            result = client.read_by_did(1)
        return result

    def test_positive_can_be_late_and_includes_request_queue_wait(self):
        result = self.run_script([(10.060, message([6, 0x62, 0, 1, 0, 200, ord("P"), 0]))])
        self.assertEqual(result["event_type"], "POS")
        self.assertEqual(result["response_time_ms"], 60)
        self.assertEqual(result["request_tx_wait_ms"], 20)
        self.assertTrue(result["response_complete"])
        self.assertFalse(result["deadline_met"])

    def test_sensor_range_failure_is_still_positive_service_response(self):
        result = self.run_script([(10.030, message([6, 0x62, 0, 1, 1, 194, ord("F"), 0]))])
        self.assertEqual((result["event_type"], result["result_char"]), ("POS", "F"))
        self.assertTrue(result["deadline_met"])

    def test_wrong_did_is_ignored_before_matching_response(self):
        result = self.run_script([
            (10.025, message([6, 0x62, 0, 2, 0, 200, ord("P"), 0])),
            (10.030, message([6, 0x62, 0, 1, 0, 200, ord("P"), 0])),
        ])
        self.assertEqual(result["event_type"], "POS")
        self.assertEqual(result["ignored_frames"], 1)
        self.assertEqual(result["response_time_ms"], 30)

    def test_wrong_negative_service_is_not_attributed_to_current_request(self):
        result = self.run_script([(10.025, message([3, 0x7F, 0x19, 0x11, 0, 0, 0, 0]))])
        self.assertEqual(result["event_type"], "NO_RESPONSE")
        self.assertFalse(result["deadline_met"])

    def test_final_nrc_completes_transport_without_becoming_pos(self):
        result = self.run_script([(10.030, message([3, 0x7F, 0x22, 0x11, 0, 0, 0, 0]))])
        self.assertEqual(result["event_type"], "NRC")
        self.assertTrue(result["response_complete"])
        self.assertTrue(result["deadline_met"])

    def test_pending_does_not_extend_the_hard_timeout(self):
        result = self.run_script([
            (10.030, message([3, 0x7F, 0x22, 0x78, 0, 0, 0, 0])),
            (10.080, message([3, 0x7F, 0x22, 0x78, 0, 0, 0, 0])),
        ], timeout=0.1)
        self.assertEqual(result["event_type"], "NO_RESPONSE")
        self.assertAlmostEqual(result["response_time_ms"], 100, places=2)

    def test_transmit_timeout_counts_as_deadline_failure(self):
        result = self.run_script([], timeout=0.01)
        self.assertEqual(result["event_type"], "TX_TIMEOUT")
        self.assertFalse(result["response_complete"])
        self.assertFalse(result["deadline_met"])

    def test_malformed_sensor_payload_is_not_positive(self):
        result = self.run_script([(10.030, message([3, 0x62, 0, 1, 0, 0, 0, 0]))])
        self.assertEqual(result["event_type"], "INVALID_RESPONSE")

    def test_wrong_consecutive_frame_sequence_is_rejected(self):
        client = batch.UdsClient(None)
        frames = [message([0x10, 10, 0x62, 0, 5, 0, 0, 0]),
                  message([0x22, 0, 0, 0, 0, 0, 0, 0])]
        self.assertEqual(client.parse_response(frames, 0x22, 5)["event_type"], "SEQUENCE_ERROR")

    def test_partial_transfer_preserves_n_cr_timeout_classification(self):
        clock = Clock()
        bus = ScriptedBus(clock, [(10.030, message([0x10, 103, 0x62, 0, 5, 0, 0, 0]))])
        client = batch.UdsClient(bus, arbiter=QueueDelay(clock), hard_timeout=0.3)
        client.N_CR_TIMEOUT = 0.05
        with patch.object(batch, "time", clock):
            result = client.read_by_did(5)
        self.assertEqual(result["event_type"], "N_CR_TIMEOUT")
        self.assertFalse(result["response_complete"])


class ArbiterTests(unittest.TestCase):
    def test_all_queued_candidates_compete_by_id(self):
        channel = uuid.uuid4().hex
        receiver = can.interface.Bus(channel=channel, interface="virtual")
        arbiter = ArbitratedBus(channel)
        try:
            with arbiter._cv:
                for arb_id in (0x700, 0x100, 0x400):
                    arbiter.submit(message([0] * 8, arb_id))
            frames = [receiver.recv(timeout=1) for _ in range(3)]
            self.assertEqual([f.arbitration_id for f in frames], [0x100, 0x400, 0x700])
        finally:
            arbiter.stop()
            receiver.shutdown()

    def test_wire_occupancy_is_clipped_and_excludes_service_overhead(self):
        arbiter = ArbitratedBus.__new__(ArbitratedBus)
        arbiter._cv = threading.Condition()
        arbiter._active = None
        arbiter._background = []
        arbiter._transmissions = [(0, 1, 0.25), (1, 2, 0.25)]
        stats = arbiter.measure_window(0.5, 1.5)
        self.assertEqual(stats["modeled_bus_load_pct"], 25)
        self.assertEqual(stats["service_overhead_pct"], 75)

    def test_due_background_sources_are_enqueued_together_and_coalesced(self):
        arbiter = ArbitratedBus.__new__(ArbitratedBus)
        arbiter._cv = threading.Condition()
        arbiter._pending = []
        import random
        arbiter._rng = random.Random(42)
        arbiter.configure_background([("a", 0x200, (0.01, 0.01)), ("b", 0x100, (0.01, 0.01))])
        arbiter._release_background(time.perf_counter() + 0.1)
        self.assertEqual(len(arbiter._pending), 2)
        self.assertGreater(arbiter.background_counts()["background_coalesced"], 0)


class SummaryTests(unittest.TestCase):
    def test_deadline_and_pos_are_separate_and_failures_do_not_bias_latency_mean(self):
        events = []
        for kind, latency, complete, on_time in [
            ("POS", 10, True, True), ("POS", 200, True, False),
            ("NO_RESPONSE", 300, False, False),
        ]:
            events.append({
                "event_type": kind, "response_time_ms": latency,
                "response_complete": complete, "deadline_met": on_time,
                "request_tx_wait_ms": 1, "deadline_ms": 100,
                "observation_s": 1, "modeled_wire_busy_s": 0.2,
                "service_busy_s": 0.3, "background_releases": 10,
                "background_coalesced": 2, "ignored_frames": 0,
            })
        result = summarize(events)
        self.assertEqual(result["pos_rate_pct"], 66.67)
        self.assertEqual(result["on_time_completion_rate_pct"], 33.33)
        self.assertEqual(result["deadline_miss_rate_pct"], 66.67)
        self.assertEqual(result["avg_completed_response_ms"], 105)
        self.assertEqual(result["modeled_bus_load_pct"], 20)


class IntegrationTests(unittest.TestCase):
    def test_fault_free_fixed_sensors_complete_all_seven_request_kinds(self):
        events = batch.run_scenario("test_baseline", {}, background_presets=[],
                                    request_count=7, hard_timeout=1, deadline_ms=1000,
                                    measure_load=True, sensor_values={1: 200, 2: 200, 3: 200, 4: 2000})
        self.assertEqual([e["event_type"] for e in events], ["POS"] * 7)
        self.assertTrue(all(e["deadline_met"] for e in events))
        self.assertTrue(all(0 <= e["modeled_bus_load_pct"] <= 100 for e in events))

    def test_transport_failure_is_followed_by_a_fresh_session(self):
        events = batch.run_scenario("test_drop", {"drop_cf_prob": 1}, background_presets=[],
                                    request_count=14, hard_timeout=0.2, measure_load=True,
                                    sensor_values={1: 200, 2: 200, 3: 200, 4: 2000})
        failures = [i for i, e in enumerate(events) if not e["response_complete"]]
        self.assertEqual(len(failures), 2)
        for index in failures:
            if index + 1 < len(events):
                self.assertGreater(events[index + 1]["session_index"], events[index]["session_index"])
                self.assertEqual(events[index + 1]["event_type"], "POS")

    def test_timed_out_request_cannot_supply_next_same_did_response(self):
        original_ecu = batch.VirtualECU
        created = []

        def ecu_factory(**kwargs):
            created.append(kwargs["channel"])
            kwargs["fault_injection"] = {"response_delay_range": (0.2, 0.2)} if len(created) == 1 else {}
            return original_ecu(**kwargs)

        with patch.object(batch, "VirtualECU", side_effect=ecu_factory), \
                patch.object(batch, "build_request_plan", return_value=[("did", 1), ("did", 1)]):
            events = batch.run_scenario("test_late", {}, background_presets=[], request_count=2,
                                        hard_timeout=0.1, deadline_ms=100, measure_load=True,
                                        sensor_values={1: 200})
        self.assertEqual(events[0]["event_type"], "NO_RESPONSE")
        self.assertEqual(events[1]["event_type"], "POS")
        self.assertEqual(len(set(created)), 2)


if __name__ == "__main__":
    unittest.main()
