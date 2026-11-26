# SPDX-License-Identifier: GPL-3.0-only OR MIT
import time

import cv2 as cv
import numpy as np

from world_vision import load_gray, peak_template, resize_templates


MOVING_MODES = ("walk", "run", "dash", "jump", "climb", "glide", "spirit")
PLAYABLE_SCENES = ("open_world", "instance", "gameplay")


def select_target(state, target, gray, templates, prior_center=None):
    kind = target["kind"]
    region = target["region"]
    if kind == "template":
        match = peak_template(gray, templates, region)
        return {"kind": "template", **match} if match else None
    candidates = state["enemy_bars"] if kind == "enemy" else state["quest_targets"]
    left, top, width, height = region
    bounded = [
        hit for hit in candidates if hit["kind"] == kind
        and left <= hit["center"][0] <= left + width and top <= hit["center"][1] <= top + height
    ]
    if prior_center is not None:
        def displacement(hit):
            x, y = hit["center"]
            return abs(x - prior_center[0]) + abs(y - prior_center[1])

        nearby = [hit for hit in bounded if displacement(hit) < .22]
        if not nearby:
            return None
        return max(nearby, key=lambda hit: hit["confidence"] - .85 * displacement(hit))
    if not bounded:
        return None
    return max(bounded, key=lambda hit: hit["confidence"] - .15 * abs(hit["center"][0] - .5))


def motion_decision(phase, state, target, aligned_frames):
    mode = phase["mode"]
    if mode == "dialogue":
        return {"state": "complete" if state["scene"] == "dialogue" else "waiting"}
    if state["scene"] not in PLAYABLE_SCENES:
        return {"state": "unknown_scene"}
    if mode in ("glide", "spirit") and state["motion"] != "fly":
        return {"state": "glide_not_detected"}
    if mode == "climb" and state["motion"] != "climb":
        return {"state": "climb_not_detected"}
    if target is None or target["confidence"] < phase["confidence"]:
        return {"state": "target_lost"}

    dx = target["center"][0] - phase["aim"][0]
    dy = target["center"][1] - phase["aim"][1]
    tolerance = phase["tolerance"]
    yaw = int(np.clip(round(phase["yaw_gain"] * dx), -72, 72))
    pitch = int(np.clip(round(phase["pitch_gain"] * dy), -48, 48))
    screen_aim = mode in ("aim", "shoot", "glide", "spirit", "combat")
    aligned = abs(dx) <= tolerance and (not screen_aim or abs(dy) <= tolerance)
    if aligned and aligned_frames >= 1:
        if mode == "aim":
            return {"state": "complete"}
        if mode == "shoot":
            return {"state": "shoot"}
        if mode in MOVING_MODES and target["size"][0] >= phase["stop_width"]:
            return {"state": "complete"}

    keys, buttons = [], []
    if mode in MOVING_MODES and abs(dx) < .13:
        keys.append("w")
        if mode in ("run", "dash"):
            keys.append("shift")
        if mode == "jump":
            keys.append("space")
        if mode == "spirit":
            if dy < -tolerance:
                keys.append("space")
            elif aligned:
                buttons.append("right")
    return {"state": "tracking", "keys": keys, "buttons": buttons,
            "yaw": yaw, "pitch": pitch if screen_aim else 0, "aligned": aligned,
            "advance": bool(keys)}


def prepare_phases(phases, templates_dir, view_height, bounded, normalized_region):
    if not isinstance(phases, list) or not 1 <= len(phases) <= 12:
        raise ValueError("phases 需要 1 至 12 个阶段")
    result = []
    modes = MOVING_MODES + ("aim", "shoot", "combat", "dialogue")
    factor = 540 / view_height

    def load_templates(name):
        image = load_gray(templates_dir(name))
        if image is None or image.std() < 3:
            raise ValueError("目标或提示模板没有可识别纹理")
        return resize_templates(image, (.75 * factor, factor, 1.25 * factor, 1.55 * factor))

    for phase in phases:
        if not isinstance(phase, dict) or phase.get("mode") not in modes:
            raise ValueError("场景阶段类型无效")
        mode = phase["mode"]
        target = phase.get("target", {"kind": "enemy"} if mode == "combat" else {})
        if not isinstance(target, dict):
            raise ValueError("target 需要目标描述")
        kind = target.get("kind")
        if mode != "dialogue" and kind not in ("blue_quest", "gold_quest", "enemy", "template"):
            raise ValueError("目标仅支持任务标记、敌人血条或本地模板")
        if mode == "combat" and kind != "enemy":
            raise ValueError("战斗阶段需要敌人血条反馈")
        region = normalized_region(target.get("region", (0, 0, 1, 1)))
        images = None
        if kind == "template":
            images = load_templates(target.get("name"))
        aim = phase.get("aim", (.5, .43))
        if not isinstance(aim, (tuple, list)) or len(aim) != 2:
            raise ValueError("aim 需要两个窗口比例")
        aim = [bounded(point, .12, .88, "aim") for point in aim]
        cues = {}
        for label in ("entry_cue", "success_cue", "failure_cue"):
            cue = phase.get(label)
            if cue is None:
                continue
            if not isinstance(cue, dict):
                raise ValueError("视觉提示需要模板名和可选区域")
            cues[label] = {
                "images": load_templates(cue.get("name")),
                "region": normalized_region(cue.get("region", (0, 0, 1, 1))),
                "confidence": bounded(cue.get("confidence", .87), .70, .99, "提示置信度"),
            }
        result.append({
            "mode": mode, "target": {"kind": kind, "region": region}, "templates": images,
            "aim": aim,
            "ms": bounded(phase.get("timeout_ms", 5000), 300, 45000, "timeout_ms", integer=True),
            "confidence": bounded(phase.get("confidence", .84 if mode == "combat" else .80), .65, .99, "confidence"),
            "tolerance": bounded(phase.get("tolerance", .035), .01, .12, "tolerance"),
            "stop_width": bounded(phase.get("stop_width", .045), .012, .55, "stop_width"),
            "yaw_gain": bounded(phase.get("yaw_gain", 290), 45, 950, "yaw_gain"),
            "pitch_gain": bounded(phase.get("pitch_gain", 235), 30, 750, "pitch_gain"),
            "charge_ms": bounded(phase.get("charge_ms", 100), 45, 1450, "charge_ms", integer=True),
            "stall_ms": bounded(phase.get("stall_ms", 1200), 220, 7000, "stall_ms", integer=True),
            "attack_keys": phase.get("attack_keys", []),
            "cues": cues,
            "jump_retries": bounded(phase.get("jump_retries", 0), 0, 2, "jump_retries", integer=True),
        })
    if sum(phase["ms"] for phase in result) > 120000:
        raise ValueError("单次任务各阶段合计最多 120 秒")
    for phase in result:
        if not isinstance(phase["attack_keys"], list) or any(
            key not in ("1", "2", "3", "4", "e", "q", "space") for key in phase["attack_keys"]
        ) or len(set(phase["attack_keys"])) != len(phase["attack_keys"]):
            raise ValueError("战斗动作限角色切换、元素战技、元素爆发和跳跃")
    return result


def cue_visible(gray, cue):
    if cue is None:
        return False
    match = peak_template(gray, cue["images"], cue["region"])
    return bool(match and match["confidence"] >= cue["confidence"])


def run_job(control, game, phases, job_id, emit, held):
    completed = 0
    started = time.perf_counter()
    observations = 0
    measurements = []
    last_state = None
    try:
        for index, phase in enumerate(phases):
            deadline = time.perf_counter() + phase["ms"] / 1000
            missing_since = None
            aligned_frames = 0
            stable_since = None
            previous_gray = None
            attack_index = 0
            last_attack = 0.0
            combat_ever_seen = False
            tracked_center = None
            awaiting_success = False
            jump_retries = phase["jump_retries"]
            last_report = 0.0
            step_label = "timeout"
            while time.perf_counter() < deadline:
                tick = time.perf_counter()
                control.check_control(game)
                frame, capture_ms = control.grab(game)
                cues = phase["cues"]
                entry_cue = cues.get("entry_cue")
                state, _, gray = control.analyze_frame(
                    frame, detail=False, allow_unclassified_targets=bool(entry_cue),
                    target_kind=phase["target"]["kind"],
                )
                entered = cue_visible(gray, entry_cue)
                if entered and state["scene"] == "unknown":
                    state["scene"] = "gameplay"
                target = select_target(state, phase["target"], gray, phase["templates"], tracked_center)
                if target and target["confidence"] >= phase["confidence"]:
                    tracked_center = target["center"]
                decision = motion_decision(phase, state, target, aligned_frames)
                if cue_visible(gray, cues.get("failure_cue")):
                    decision = {"state": "challenge_failure_visible"}
                elif cue_visible(gray, cues.get("success_cue")):
                    decision = {"state": "complete"}
                elif awaiting_success:
                    decision = {"state": "waiting_success_cue"}
                elif entry_cue and not entered:
                    decision = {"state": "waiting_entry_cue"}
                elif decision["state"] == "complete" and "success_cue" in cues:
                    awaiting_success = True
                    decision = {"state": "waiting_success_cue"}
                step_label = decision["state"]
                measurements.append(round((time.perf_counter() - tick) * 1000, 2))
                observations += 1
                last_state = {"phase": index + 1, "mode": phase["mode"], "scene": state["scene"],
                              "motion": state["motion"], "target": target, "step": step_label,
                              "capture_ms": capture_ms, "loop_ms": measurements[-1],
                              "capture_source": control.capture_source,
                              "remaining_ms": max(0, round((deadline - tick) * 1000))}
                if tick - last_report >= .25 or step_label in ("complete", "shoot"):
                    emit(job_id, last_state)
                    last_report = tick
                if step_label == "complete":
                    held.release()
                    completed += 1
                    break
                if step_label == "shoot":
                    control.apply_step(game, ([], ["left"], phase["charge_ms"], (0, 0), None), held)
                    held.release()
                    if "success_cue" in cues:
                        awaiting_success = True
                        continue
                    completed += 1
                    last_state["step"] = "shot_dispatched"
                    break
                if step_label != "tracking":
                    held.release()
                    aligned_frames = 0
                    if step_label == "challenge_failure_visible":
                        break
                    if missing_since is None:
                        missing_since = tick
                    if tick - missing_since > .42 and step_label not in (
                        "waiting", "waiting_entry_cue", "waiting_success_cue"
                    ):
                        if phase["mode"] == "combat" and combat_ever_seen and step_label == "target_lost":
                            step_label = "no_enemy_visible"
                            if "success_cue" in cues:
                                awaiting_success = True
                                continue
                        break
                    control.exiting.wait(.025)
                    continue
                missing_since = None
                aligned_frames = aligned_frames + 1 if decision["aligned"] else 0
                keys, buttons = decision["keys"], decision["buttons"]
                if phase["mode"] == "combat":
                    combat_ever_seen = True
                    if abs(target["center"][0] - phase["aim"][0]) < .14 and tick - last_attack >= .34:
                        keys = [phase["attack_keys"][attack_index % len(phase["attack_keys"])]] if phase["attack_keys"] else []
                        buttons = ["left"] if not keys else []
                        attack_index += 1
                        last_attack = tick
                if decision["advance"]:
                    small = cv.resize(gray[100:435, 180:780], (96, 54), interpolation=cv.INTER_AREA)
                    unchanged = previous_gray is not None and float(cv.absdiff(small, previous_gray).mean()) < 1.25
                    if unchanged and stable_since is None:
                        stable_since = tick
                    elif not unchanged:
                        stable_since = None
                    previous_gray = small
                    if stable_since is not None and 1000 * (tick - stable_since) >= phase["stall_ms"]:
                        if jump_retries and phase["mode"] in ("walk", "run", "dash"):
                            jump_retries -= 1
                            stable_since = None
                            previous_gray = None
                            control.apply_step(game, (["w", "space"], [], 170, (0, 0), None), held)
                            continue
                        step_label = "stalled"
                        break
                else:
                    stable_since = None
                    previous_gray = None
                tick_ms = 12 if measurements[-1] > 22 else 22
                control.apply_step(game, (keys, buttons, tick_ms, (decision["yaw"], decision["pitch"]), None), held)
            held.release()
            if completed != index + 1:
                return job_result("stopped", completed, observations, measurements, started, last_state,
                                  step_label if time.perf_counter() < deadline else "timeout")
    finally:
        held.release()
    return job_result("completed", completed, observations, measurements, started, last_state)


def job_result(status, completed, observations, measurements, started, latest, reason=None):
    window = measurements[-240:]
    return {
        "status": status, "completed_phases": completed, "observations": observations,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
        "median_frame_loop_ms": round(float(np.median(window)), 1) if window else None,
        "p95_frame_loop_ms": round(float(np.percentile(window, 95)), 1) if window else None,
        "reason": reason,
        "latest": latest,
    }
