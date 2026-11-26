# SPDX-License-Identifier: GPL-3.0-only OR MIT
from pathlib import Path
import math

import cv2 as cv
import numpy as np


# 识别在固定大小的画面上运行，坐标仍返回客户区比例。
FRAME_WIDTH = 960
FRAME_HEIGHT = 540
COMMON_ASSETS = Path("GameTask/Common/Element/Assets/1920x1080")
DIALOGUE_ASSETS = Path("GameTask/AutoSkip/Assets/1920x1080")
MAP_ASSETS = Path("GameTask/QuickTeleport/Assets/1920x1080")
UI_TEMPLATES = {
    "prompt": (COMMON_ASSETS, "prompt_dialog_left_bottom_star.png", (.09, .42, .42, .53), .80),
    "map_scale": (MAP_ASSETS, "MapScaleButton.png", (.004, .34, .075, .31), .77),
    "map_close": (MAP_ASSETS, "MapCloseButton.png", (.92, .006, .075, .105), .79),
    "map_settings": (MAP_ASSETS, "MapSettingsButton.png", (.006, .89, .073, .11), .79),
    "paimon": (COMMON_ASSETS, "paimon_menu.png", (0, 0, .25, .25), .78),
    "inventory": (COMMON_ASSETS, "inventory.png", (.74, 0, .26, .105), .81),
    "domain_exit": (COMMON_ASSETS, "btn_exit_door.png", (0, 0, .25, .24), .78),
    "space": (COMMON_ASSETS, "key_space.png", (.80, .87, .18, .13), .79),
    "climb_exit": (COMMON_ASSETS, "key_x.png", (.80, .87, .18, .13), .82),
    "blue_quest": (COMMON_ASSETS, "blue_track_point_28x.png", (.15, .03, .70, .76), .72),
    "dialogue": (DIALOGUE_ASSETS, "disabled_ui.png", (0, 0, .3334, .125), .82),
    "dialogue_exit": (DIALOGUE_ASSETS, "chat_exit.png", (0, 0, .35, .13), .83),
    "dialogue_option": (DIALOGUE_ASSETS, "icon_option.png", (.50, .13, .33, .68), .83),
}


def load_gray(path):
    return cv.imdecode(np.fromfile(path, dtype=np.uint8), cv.IMREAD_GRAYSCALE)


def region_pixels(region, width, height):
    x, y, w, h = region
    return round(x * width), round(y * height), round((x + w) * width), round((y + h) * height)


def resize_templates(template, scales):
    height, width = template.shape
    return tuple(cv.resize(template, (max(3, round(width * scale)), max(3, round(height * scale))),
                           interpolation=cv.INTER_AREA if scale < 1 else cv.INTER_LINEAR)
                 for scale in scales)


def peak_template(gray, templates, region=None):
    height, width = gray.shape
    x, y, right, bottom = region_pixels(region or (0, 0, 1, 1), width, height)
    selected = gray[y:bottom, x:right]
    best = None
    for template in templates:
        th, tw = template.shape
        if selected.shape[0] < th or selected.shape[1] < tw:
            continue
        _, score, _, (left, top) = cv.minMaxLoc(cv.matchTemplate(selected, template, cv.TM_CCOEFF_NORMED))
        if not math.isfinite(score) or (best is not None and score <= best["confidence"]):
            continue
        best = {"center": [(x + left + tw / 2) / width, (y + top + th / 2) / height],
                "size": [tw / width, th / height], "confidence": float(score)}
    return best


def contour_targets(mask, region, kind):
    height, width = mask.shape
    x, y, right, bottom = region_pixels(region, width, height)
    cropped = mask[y:bottom, x:right]
    if kind == "enemy":
        cropped = cv.morphologyEx(cropped, cv.MORPH_CLOSE, np.ones((1, 9), np.uint8))
    contours, _ = cv.findContours(cropped, cv.RETR_EXTERNAL, cv.CHAIN_APPROX_SIMPLE)
    hits = []
    for contour in contours:
        left, top, w, h = cv.boundingRect(contour)
        if kind == "enemy":
            if not (12 <= w <= width * .62 and 2 <= h <= 18 and w / h >= 4.7):
                continue
            fill = cv.countNonZero(cropped[top:top + h, left:left + w]) / (w * h)
            score = .54 + .22 * fill + min(.16, w / 210)
        else:
            if not (8 <= w <= 36 and 9 <= h <= 38 and .62 <= w / h <= 1.35):
                continue
            shape = cv.approxPolyDP(cv.convexHull(contour), .09 * cv.arcLength(contour, True), True)
            extent = cv.contourArea(contour) / (w * h)
            if not (4 <= len(shape) <= 6 and .24 <= extent <= .76):
                continue
            score = .76 + .16 * (1 - abs(1 - w / h))
        hits.append({"kind": kind, "center": [(x + left + w / 2) / width, (y + top + h / 2) / height],
                     "size": [w / width, h / height], "confidence": round(float(score), 3)})
    return sorted(hits, key=lambda hit: hit["confidence"], reverse=True)[:6]


def minimap_heading(hsv):
    height, width = hsv.shape[:2]
    x, y, right, bottom = region_pixels((.017, .023, .135, .165), width, height)
    view = hsv[y:bottom, x:right]
    cyan = cv.inRange(view, (83, 155, 172), (107, 255, 255))
    contours, _ = cv.findContours(cyan, cv.RETR_EXTERNAL, cv.CHAIN_APPROX_SIMPLE)
    candidates = []
    center = np.array([view.shape[1] / 2, view.shape[0] / 2])
    for contour in contours:
        area = cv.contourArea(contour)
        if not 12 <= area <= 170:
            continue
        hull = cv.convexHull(contour)
        vertices = cv.approxPolyDP(hull, .10 * cv.arcLength(hull, True), True).reshape(-1, 2)
        if len(vertices) != 3:
            continue
        middle = vertices.mean(axis=0)
        distances = [float(np.linalg.norm(vertex - middle)) for vertex in vertices]
        tip = vertices[int(np.argmax(distances))]
        dx, dy = tip - middle
        if math.hypot(dx, dy) < 3:
            continue
        offset = float(np.linalg.norm(middle - center))
        candidates.append((offset, (math.degrees(math.atan2(dx, -dy)) + 360) % 360))
    if not candidates:
        return None
    offset, heading = min(candidates)
    if offset > 32:
        return None
    return {"degrees": round(heading, 1), "confidence": round(.9 - .22 * offset / 32, 2)}


class WorldVision:
    def __init__(self, bettergi_path):
        self.templates = {}
        self.missing_assets = []
        for name, (folder, file, region, threshold) in UI_TEMPLATES.items():
            path = Path(bettergi_path) / folder / file
            template = load_gray(path) if path.is_file() else None
            if template is None:
                self.missing_assets.append(name)
                continue
            self.templates[name] = (resize_templates(template, (.47, .50, .54)), region, threshold)

    def match_ui(self, gray, name):
        if name not in self.templates:
            return None
        templates, region, threshold = self.templates[name]
        result = peak_template(gray, templates, region)
        if result is None or result["confidence"] < threshold:
            return None
        return {**result, "kind": name, "confidence": round(result["confidence"], 3)}

    def analyze(self, frame, *, detail=True, allow_unclassified_targets=False, target_kind=None):
        resized = cv.resize(frame, (FRAME_WIDTH, FRAME_HEIGHT), interpolation=cv.INTER_AREA)
        gray = cv.cvtColor(resized, cv.COLOR_BGR2GRAY)
        ui = {}

        def check(name):
            result = self.match_ui(gray, name)
            if result:
                ui[name] = result
            return result

        def map_visible():
            return check("map_close") and (check("map_scale") or check("map_settings"))

        if check("prompt"):
            scene = "map" if map_visible() else "prompt"
        elif check("dialogue"):
            scene = "dialogue"
            check("dialogue_option")
        elif check("inventory"):
            if check("domain_exit"):
                scene = "instance"
            elif check("paimon"):
                scene = "open_world"
            else:
                scene = "gameplay"
        elif check("dialogue_exit") and check("dialogue_option"):
            scene = "dialogue"
        elif map_visible():
            scene = "map"
        else:
            scene = "unknown"

        playable = scene in ("instance", "open_world", "gameplay")
        motion = None
        if playable:
            motion = ("climb" if check("climb_exit") else "fly") if check("space") else "normal"
        look_for_targets = playable or (allow_unclassified_targets and scene == "unknown")
        quest, enemies, heading = [], [], None
        if look_for_targets:
            hsv = cv.cvtColor(resized, cv.COLOR_BGR2HSV)
            if target_kind in (None, "blue_quest"):
                blue = check("blue_quest")
                if blue:
                    width, height = blue["size"]
                    x, y = blue["center"]
                    left, top, right, bottom = region_pixels(
                        (x - width / 2, y - height / 2, width, height), FRAME_WIDTH, FRAME_HEIGHT
                    )
                    region = hsv[top:bottom, left:right]
                    color = cv.inRange(region, (82, 120, 176), (120, 255, 255))
                    if cv.countNonZero(color) >= .09 * color.size:
                        quest.append(blue)
                    else:
                        ui.pop("blue_quest", None)
            if target_kind in (None, "gold_quest"):
                yellow = cv.inRange(hsv, (15, 150, 166), (39, 255, 255))
                quest.extend(contour_targets(yellow, (.15, .075, .70, .70), "gold_quest"))
            if target_kind in (None, "enemy"):
                red = cv.bitwise_or(cv.inRange(hsv, (0, 152, 115), (7, 255, 255)),
                                    cv.inRange(hsv, (174, 152, 115), (179, 255, 255)))
                enemies = contour_targets(red, (.10, .05, .80, .66), "enemy")
            if detail and playable:
                heading = minimap_heading(hsv)
        result = {"scene": scene, "motion": motion, "quest_targets": quest, "enemy_bars": enemies,
                  "signals": {name: result["confidence"] for name, result in ui.items()},
                  "dialogue_choice": bool("dialogue_option" in ui and scene == "dialogue")}
        if detail:
            result["minimap_heading"] = heading
            result["missing_assets"] = self.missing_assets
        return result, resized, gray
