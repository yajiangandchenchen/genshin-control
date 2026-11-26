# SPDX-License-Identifier: GPL-3.0-only OR MIT
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import sys
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import cv2 as cv
import numpy as np


BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE))
from game_control import GameControl, Interrupted, bounded, bundled_engine, normalized_region
from local_policy import motion_decision, prepare_phases, run_job, select_target
from world_vision import WorldVision


RUNTIME = bundled_engine()
VISION = WorldVision(RUNTIME)


def paste(frame, name, x, y):
    image = VISION.templates[name][0][1]
    height, width = image.shape
    frame[y:y + height, x:x + width] = cv.cvtColor(image, cv.COLOR_GRAY2BGR)


def synthetic(scene="open_world", motion="normal", marker=None, enemy=False):
    frame = np.full((540, 960, 3), (53, 49, 39), np.uint8)
    if scene == "dialogue":
        paste(frame, "dialogue", 24, 18)
        paste(frame, "dialogue_option", 590, 238)
        return frame
    if scene == "unknown":
        return frame
    paste(frame, "inventory", 880, 20)
    paste(frame, "domain_exit" if scene == "instance" else "paimon", 26, 15)
    if motion in ("fly", "climb"):
        paste(frame, "space", 842, 505)
    if motion == "climb":
        paste(frame, "climb_exit", 790, 506)
    if marker:
        x, y, r = marker
        cv.fillConvexPoly(frame, np.array([[x, y-r], [x+r, y], [x, y+r], [x-r, y]]), (4, 185, 250))
    if enemy:
        cv.rectangle(frame, (432, 115), (479, 119), (12, 8, 239), cv.FILLED)
    cv.fillConvexPoly(frame, np.array([[77, 45], [70, 65], [84, 65]]), (220, 213, 20))
    return frame


class Held:
    def __init__(self):
        self.keys = ()
        self.buttons = ()
        self.releases = 0

    def release(self):
        self.keys = ()
        self.buttons = ()
        self.releases += 1


class Replayer:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.current = None
        self.capture_source = "离线回放"
        self.exiting = threading.Event()
        self.actions = []

    def check_control(self, game):
        pass

    def grab(self, game):
        self.current = next(self.frames, self.current)
        return self.current, 0.0

    def analyze_frame(self, frame, detail=True, allow_unclassified_targets=False, target_kind=None):
        return VISION.analyze(frame, detail=detail, allow_unclassified_targets=allow_unclassified_targets,
                              target_kind=target_kind)

    def apply_step(self, game, step, held):
        keys, buttons, duration, delta, _ = step
        self.actions.append((tuple(keys), tuple(buttons), delta))
        held.keys = tuple(keys)
        held.buttons = tuple(buttons)
        time.sleep(min(0.018, duration / 1000))


def compile_plan(phases):
    return prepare_phases(phases, lambda _: None, 540, bounded, normalized_region)


class ControlReplay(unittest.TestCase):
    def test_isolated_windows_lease_and_stop(self):
        namespace = "GenshinCodexTest-" + uuid4().hex
        first = GameControl(BRIDGE.parent / ".local" / "state", None, namespace=namespace)
        second = GameControl(BRIDGE.parent / ".local" / "state", None, namespace=namespace)
        try:
            with ThreadPoolExecutor(max_workers=1) as executor, first.exclusive():
                self.assertRaises(Interrupted, executor.submit(second.claim_bettergi).result)
            first.claim_bettergi()
            self.assertTrue(second.bettergi_claimed())
            with self.assertRaises(Interrupted):
                second.claim_bettergi()
            with patch("game_control.bettergi_running", return_value=False):
                first.release_bettergi()
            self.assertFalse(second.bettergi_claimed())
            second.stop()
            self.assertTrue(first.stopped())
        finally:
            first.close_handles()
            second.close_handles()

    def test_reuses_bettergi_assets_for_hud_and_motion(self):
        self.assertEqual(VISION.missing_assets, [])
        world, _, _ = VISION.analyze(synthetic(marker=(580, 215, 11), enemy=True))
        self.assertEqual(world["scene"], "open_world")
        self.assertEqual(world["motion"], "normal")
        self.assertIsNotNone(world["minimap_heading"])
        self.assertTrue(world["quest_targets"])
        self.assertTrue(world["enemy_bars"])
        dungeon, _, _ = VISION.analyze(synthetic("instance", "fly"))
        self.assertEqual((dungeon["scene"], dungeon["motion"]), ("instance", "fly"))
        climb, _, _ = VISION.analyze(synthetic("instance", "climb"))
        self.assertEqual(climb["motion"], "climb")
        dialog, _, _ = VISION.analyze(synthetic("dialogue"))
        self.assertEqual(dialog["scene"], "dialogue")
        self.assertTrue(dialog["dialogue_choice"])

    def test_uses_stable_feedback_before_advancing_or_shooting(self):
        target = {"kind": "gold_quest"}
        phases = compile_plan([
            {"mode": "walk", "target": target, "stop_width": .025, "timeout_ms": 2000},
            {"mode": "shoot", "target": target, "aim": [.5, .40], "timeout_ms": 2000},
        ])
        frames = [synthetic(marker=(560, 216, 9)), synthetic(marker=(490, 216, 13)),
                  synthetic(marker=(480, 216, 13)), synthetic(marker=(480, 216, 13))]
        control, held = Replayer(frames), Held()
        reported = []
        outcome = run_job(control, {}, phases, "offline", lambda *args: reported.append(args), held)
        self.assertEqual(outcome["completed_phases"], 2)
        self.assertTrue(any(keys == ("w",) for keys, _, _ in control.actions))
        self.assertEqual(sum(buttons == ("left",) for _, buttons, _ in control.actions), 1)
        self.assertEqual(held.buttons, ())
        self.assertTrue(reported)

    def test_releases_when_state_is_unknown_and_detects_stall(self):
        phases = compile_plan([{"mode": "walk", "target": {"kind": "gold_quest"},
                                "stop_width": .10, "stall_ms": 230, "timeout_ms": 1600}])
        control, held = Replayer([synthetic("unknown")]), Held()
        outcome = run_job(control, {}, phases, "offline", lambda *args: None, held)
        self.assertEqual(outcome["reason"], "unknown_scene")
        self.assertEqual(control.actions, [])
        control, held = Replayer([synthetic(marker=(478, 220, 10))]), Held()
        outcome = run_job(control, {}, phases, "offline", lambda *args: None, held)
        self.assertEqual(outcome["reason"], "stalled")
        self.assertEqual(held.keys, ())

    def test_gliding_rejects_ground_and_spirit_uses_visual_pitch(self):
        phase = compile_plan([{"mode": "spirit", "target": {"kind": "gold_quest"}}])[0]
        point = {"center": [.5, .26], "size": [.021, .03], "confidence": .94}
        self.assertEqual(motion_decision(phase, {"scene": "instance", "motion": "normal"}, point, 0)["state"],
                         "glide_not_detected")
        decision = motion_decision(phase, {"scene": "instance", "motion": "fly"}, point, 0)
        self.assertEqual(decision["keys"], ["w", "space"])
        self.assertLess(decision["pitch"], 0)
        self.assertEqual(motion_decision(phase, {"scene": "instance", "motion": "fly"}, None, 0)["state"],
                         "target_lost")

    def test_does_not_switch_to_a_different_marker_mid_phase(self):
        tracked = {"kind": "gold_quest", "center": [.56, .46], "confidence": .84}
        different = {"kind": "gold_quest", "center": [.29, .25], "confidence": .97}
        target = {"kind": "gold_quest", "region": (0, 0, 1, 1)}
        state = {"quest_targets": [different, tracked]}
        self.assertIs(select_target(state, target, None, None, [.53, .48]), tracked)
        self.assertIsNone(select_target({"quest_targets": [different]}, target, None, None, [.53, .48]))

    def test_waits_for_visual_start_and_result_markers(self):
        phase = compile_plan([{"mode": "aim", "target": {"kind": "gold_quest"},
                               "aim": [.5, .40], "timeout_ms": 1600}])[0]
        rng = np.random.default_rng(17)
        entry = rng.integers(20, 230, (19, 28), dtype=np.uint8)
        success = rng.integers(20, 230, (24, 36), dtype=np.uint8)
        phase["cues"] = {
            "entry_cue": {"images": [entry], "region": (.2, .02, .2, .18), "confidence": .9},
            "success_cue": {"images": [success], "region": (.6, .62, .2, .16), "confidence": .9},
        }
        missing = synthetic(marker=(520, 216, 13))
        ready = synthetic(marker=(480, 216, 13))
        ready[40:59, 218:246] = cv.cvtColor(entry, cv.COLOR_GRAY2BGR)
        passed = ready.copy()
        passed[350:374, 631:667] = cv.cvtColor(success, cv.COLOR_GRAY2BGR)
        control, held = Replayer([missing, ready, ready, ready, passed]), Held()
        updates = []
        outcome = run_job(control, {}, [phase], "offline", lambda _, packet: updates.append(packet), held)
        self.assertEqual(updates[0]["step"], "waiting_entry_cue")
        self.assertEqual(updates[-1]["step"], "complete")
        self.assertEqual(outcome["completed_phases"], 1)
        self.assertEqual(held.keys, ())


if __name__ == "__main__":
    unittest.main()
