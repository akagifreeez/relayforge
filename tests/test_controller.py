#!/usr/bin/env python3
"""Offline, deterministic tests for the RelayForge health state machine and
failover decision logic. No network, no MediaMTX, no OBS — synthetic snapshots
drive PathHealth.compute_state() and decide() directly.

Run from the repo root:
    python -m unittest discover -s tests -t .
"""
import os
import sys
import time
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import controller as C  # noqa: E402


def fresh_path(name, **kw):
    p = C.PathHealth(name)
    for k, v in kw.items():
        setattr(p, k, v)
    return p


class ComputeStateTests(unittest.TestCase):
    """compute_state() maps health fields -> GOOD / DEGRADED / DEAD."""

    def good(self):
        # ready, healthy bitrate, no rtt/loss problems
        return fresh_path("x", ready=True, bitrate=1000.0, offline=0, freeze=0)

    def test_good(self):
        p = self.good(); p.compute_state()
        self.assertEqual(p.state, "GOOD")

    def test_dead_when_offline_hysteresis(self):
        p = self.good(); p.offline = C.OFFLINE_POLLS
        p.compute_state()
        self.assertEqual(p.state, "DEAD")

    def test_dead_when_frozen(self):
        p = self.good(); p.freeze = C.FREEZE_POLLS
        p.compute_state()
        self.assertEqual(p.state, "DEAD")

    def test_dead_when_not_ready(self):
        p = self.good(); p.ready = False
        p.compute_state()
        self.assertEqual(p.state, "DEAD")

    def test_degraded_low_bitrate_needs_hysteresis(self):
        p = self.good(); p.bitrate = C.DEGRADE_BITRATE_KBPS - 1
        # one bad poll is not enough — needs DEGRADE_POLLS consecutive
        p.compute_state()
        self.assertEqual(p.state, "GOOD")
        for _ in range(C.DEGRADE_POLLS - 1):
            p.compute_state()
        self.assertEqual(p.state, "DEGRADED")

    def test_degraded_high_rtt(self):
        p = self.good(); p.rtt = C.DEGRADE_RTT_MS + 1
        for _ in range(C.DEGRADE_POLLS):
            p.compute_state()
        self.assertEqual(p.state, "DEGRADED")

    def test_degraded_high_loss(self):
        p = self.good(); p.loss = C.DEGRADE_LOSS_PCT + 1
        for _ in range(C.DEGRADE_POLLS):
            p.compute_state()
        self.assertEqual(p.state, "DEGRADED")

    def test_degrade_counter_resets_on_recovery(self):
        p = self.good(); p.bitrate = 10.0
        for _ in range(C.DEGRADE_POLLS):
            p.compute_state()
        self.assertEqual(p.state, "DEGRADED")
        p.bitrate = 1000.0          # link recovers
        p.compute_state()
        self.assertEqual(p.state, "GOOD")
        self.assertEqual(p.degrade, 0)


class DecideTests(unittest.TestCase):
    """decide() picks the single ACTIVE link from priority + state + cooldown."""

    def setUp(self):
        C.LOGFILE = os.path.join(tempfile.gettempdir(), "relayforge-test.log")
        C._obs = None                 # never touch OBS in tests
        C.PRIORITY = ["linkA", "linkB"]
        C.paths = {}
        C.active = None
        C.last_switch = 0.0

    def set_links(self, **states):
        now = time.time()
        C.paths = {n: fresh_path(n, state=st, ready=(st != "DEAD"), last_seen=now)
                   for n, st in states.items()}

    def test_initial_pick_is_highest_priority_good(self):
        self.set_links(linkA="GOOD", linkB="GOOD")
        C.decide()
        self.assertEqual(C.active, "linkA")

    def test_failover_on_dead(self):
        self.set_links(linkA="DEAD", linkB="GOOD")
        C.active = "linkA"; C.last_switch = 0.0
        C.decide()
        self.assertEqual(C.active, "linkB")

    def test_failover_to_degraded_when_no_good(self):
        self.set_links(linkA="DEAD", linkB="DEGRADED")
        C.active = "linkA"; C.last_switch = 0.0
        C.decide()
        self.assertEqual(C.active, "linkB")

    def test_degraded_primary_does_not_flap_to_lower_priority_good(self):
        # linkA (primary) merely degraded, linkB GOOD but lower priority -> keep linkA
        self.set_links(linkA="DEGRADED", linkB="GOOD")
        C.active = "linkA"; C.last_switch = time.time() - 100  # past cooldown
        C.decide()
        self.assertEqual(C.active, "linkA")

    def test_recovery_returns_to_primary_after_cooldown(self):
        self.set_links(linkA="GOOD", linkB="GOOD")
        C.active = "linkB"; C.last_switch = time.time() - (C.COOLDOWN_S + 5)
        C.decide()
        self.assertEqual(C.active, "linkA")

    def test_no_recovery_within_cooldown(self):
        self.set_links(linkA="GOOD", linkB="GOOD")
        C.active = "linkB"; C.last_switch = time.time()  # just switched
        C.decide()
        self.assertEqual(C.active, "linkB")

    def test_stale_link_excluded_from_alive(self):
        # linkA GOOD but not seen for >5s -> not eligible; fail over to linkB
        now = time.time()
        C.paths = {
            "linkA": fresh_path("linkA", state="GOOD", ready=True, last_seen=now - 10),
            "linkB": fresh_path("linkB", state="GOOD", ready=True, last_seen=now),
        }
        C.active = "linkA"; C.last_switch = 0.0
        C.decide()
        self.assertEqual(C.active, "linkB")

    def test_no_source(self):
        C.paths = {}
        C.active = None
        C.decide()
        self.assertIsNone(C.active)


if __name__ == "__main__":
    unittest.main(verbosity=2)
