# SPDX-License-Identifier: GPL-3.0-only
"""Domain tests: state machine transitions and Store transaction behaviour."""
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from domain.schemas.core import Event, Run
from domain.repositories.store import Store
from domain.state_machine.machine import can_transition, transition


class TestStateMachine(unittest.TestCase):
    def test_happy_path(self):
        s = "PLANNED"
        for nxt in ("ASSET_READY", "PROMPT_READY", "QUEUED", "RENDERING",
                    "GENERATED", "SCORING", "ACCEPTED"):
            s = transition(s, nxt)
        self.assertEqual(s, "ACCEPTED")

    def test_reuse_path(self):
        s = transition("ASSET_READY", "NORMALIZING")
        s = transition(s, "SCORING")
        self.assertTrue(can_transition(s, "ACCEPTED"))

    def test_repair_loop(self):
        s = transition("SCORING", "REPAIRING")
        self.assertEqual(transition(s, "PROMPT_READY"), "PROMPT_READY")
        self.assertEqual(transition("SCORING", "REPAIRING"), "REPAIRING")

    def test_illegal(self):
        with self.assertRaises(ValueError):
            transition("PLANNED", "ACCEPTED")
        with self.assertRaises(ValueError):
            transition("ACCEPTED", "PLANNED")

    def test_human_review(self):
        self.assertTrue(can_transition("SCORING", "HUMAN_REVIEW"))
        self.assertTrue(can_transition("HUMAN_REVIEW", "ACCEPTED"))


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "t.db"))

    def tearDown(self):
        self.store.close()

    def test_run_transition_with_event_and_outbox(self):
        run = Run(shot_id="s1", project_id="p1")
        self.store.put("runs", run)
        ev = Event(project_id="p1", run_id=run.run_id, type="step.queued", actor="test")
        run2 = self.store.transition_run(run.run_id, "ASSET_READY", ev)
        self.assertEqual(run2.state, "ASSET_READY")
        self.assertEqual(run2.state_version, 1)
        pending = self.store.unpublish_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].type, "step.queued")
        self.assertEqual(pending[0].seq, 1)
        self.store.mark_published(pending[0].event_id)
        self.assertEqual(self.store.unpublish_pending(), [])

    def test_illegal_transition_rolls_back(self):
        run = Run(shot_id="s1", project_id="p1")
        self.store.put("runs", run)
        ev = Event(project_id="p1", run_id=run.run_id, type="x")
        with self.assertRaises(ValueError):
            self.store.transition_run(run.run_id, "ACCEPTED", ev)
        self.assertEqual(self.store.get("runs", run.run_id).state, "PLANNED")
        self.assertEqual(self.store.events_since("p1"), [])

    def test_events_cursor(self):
        for i in range(3):
            self.store.append_event(Event(project_id="p1", type="t", summary=str(i)))
        self.assertEqual([e.seq for e in self.store.events_since("p1", seq=1)], [2, 3])


if __name__ == "__main__":
    unittest.main()


class TestStoreConcurrency(unittest.TestCase):
    """Reads and writes race across threads; sqlite must stay consistent."""

    def test_concurrent_read_write(self):
        tmp = tempfile.mkdtemp()
        store = Store(os.path.join(tmp, "race.db"))
        run = Run(shot_id="s1", project_id="p1")
        store.put("runs", run)
        errors = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    store.get("runs", run.run_id)
                    store.all("runs")
                    store.events_since("p1")
                except Exception as e:  # noqa: BLE001
                    errors.append(e)
                    stop.set()

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for i in range(30):
            store.append_event(Event(project_id="p1", type="t", summary=str(i)))
            store.put("runs", run)
        stop.set()
        for t in threads:
            t.join()
        store.close()
        self.assertEqual(errors, [])
