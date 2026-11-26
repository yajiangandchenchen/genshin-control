# SPDX-License-Identifier: GPL-3.0-only OR MIT
import argparse
import ctypes as ct
import json
import math
import os
from pathlib import Path
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from ctypes import wintypes as win

import cv2
import mss
import numpy as np
import pydirectinput as input_api
from world_vision import WorldVision, peak_template, resize_templates
from local_policy import prepare_phases, run_job

# 只读取前台窗口、可见像素和进程映像路径。
user = ct.WinDLL("user32", use_last_error=True)
kernel = ct.WinDLL("kernel32", use_last_error=True)
user.GetForegroundWindow.restype = win.HWND
user.GetWindowThreadProcessId.argtypes = (win.HWND, ct.POINTER(win.DWORD))
user.GetClientRect.argtypes = (win.HWND, ct.POINTER(win.RECT))
user.ClientToScreen.argtypes = (win.HWND, ct.POINTER(win.POINT))
user.GetAsyncKeyState.argtypes = (ct.c_int,)
user.GetAsyncKeyState.restype = ct.c_short
user.SetCursorPos.argtypes = (ct.c_int, ct.c_int)
if hasattr(user, "SetProcessDpiAwarenessContext"):
    user.SetProcessDpiAwarenessContext.argtypes = (win.HANDLE,)
    user.SetProcessDpiAwarenessContext(ct.c_void_p(-4))

kernel.OpenProcess.argtypes = (win.DWORD, win.BOOL, win.DWORD)
kernel.OpenProcess.restype = win.HANDLE
kernel.QueryFullProcessImageNameW.argtypes = (
    win.HANDLE, win.DWORD, win.LPWSTR, ct.POINTER(win.DWORD)
)
kernel.CloseHandle.argtypes = (win.HANDLE,)
kernel.CreateMutexW.argtypes = (ct.c_void_p, win.BOOL, win.LPCWSTR)
kernel.CreateMutexW.restype = win.HANDLE
kernel.CreateEventW.argtypes = (ct.c_void_p, win.BOOL, win.BOOL, win.LPCWSTR)
kernel.CreateEventW.restype = win.HANDLE
kernel.WaitForSingleObject.argtypes = (win.HANDLE, win.DWORD)
kernel.WaitForSingleObject.restype = win.DWORD
kernel.ReleaseMutex.argtypes = (win.HANDLE,)
kernel.SetEvent.argtypes = (win.HANDLE,)
kernel.ResetEvent.argtypes = (win.HANDLE,)
kernel.CreateToolhelp32Snapshot.argtypes = (win.DWORD, win.DWORD)
kernel.CreateToolhelp32Snapshot.restype = win.HANDLE


class ProcessEntry(ct.Structure):
    _fields_ = [
        ("size", win.DWORD), ("usage", win.DWORD), ("pid", win.DWORD),
        ("heap", ct.c_size_t), ("module", win.DWORD), ("threads", win.DWORD),
        ("parent", win.DWORD), ("priority", win.LONG), ("flags", win.DWORD),
        ("name", win.WCHAR * 260),
    ]


for function in (kernel.Process32FirstW, kernel.Process32NextW):
    function.argtypes = (win.HANDLE, ct.POINTER(ProcessEntry))

cv2.setNumThreads(2)
# 自己的急停可随时松开全部按键，释放时也必须能执行 SendInput。
input_api.FAILSAFE = False
ALLOWED_KEYS = frozenset(
    "w a s d q e r f t z x c v b m j i l g h n u o p tab space shift ctrl alt esc enter up down left right 1 2 3 4 5 f1 f2 f3 f4 f5 f6 f7 f8 f9 f11 f12".split()
)
BUTTONS = frozenset(("left", "right", "middle"))
INTERRUPT_KEYS = (0x11, 0x12, 0x79)  # Ctrl + Alt + F10
GAME_EXES = frozenset(("yuanshen.exe", "genshinimpact.exe"))
BGI_EXES = frozenset(("bettergi.exe", "bettergenshinimpact.exe"))
FULL_REGION = (0, 0, 1, 1)


def bounded(value, lower, upper, label, integer=False):
    expected = (int,) if integer else (int, float)
    if type(value) not in expected or not math.isfinite(value) or not lower <= value <= upper:
        raise ValueError(f"{label} 必须在 {lower} 至 {upper} 之间")
    return value


def normalized_region(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("region 需要 [左, 上, 宽, 高] 四个比例")
    left, top, width, height = [bounded(v, 0, 1, "区域坐标") for v in value]
    if width <= 0 or height <= 0 or left + width > 1.000001 or top + height > 1.000001:
        raise ValueError("区域必须完全落在游戏窗口内部")
    return left, top, width, height


def game_image_path(pid):
    process = kernel.OpenProcess(0x1000, False, pid)
    if not process:
        return ""
    try:
        capacity = win.DWORD(32768)
        buffer = ct.create_unicode_buffer(capacity.value)
        if kernel.QueryFullProcessImageNameW(process, 0, buffer, ct.byref(capacity)):
            return buffer.value
        return ""
    finally:
        kernel.CloseHandle(process)


def bettergi_running():
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ct.c_void_p(-1).value:
        raise OSError("读取 BetterGI 运行状态失败")
    try:
        entry = ProcessEntry(ct.sizeof(ProcessEntry))
        found = kernel.Process32FirstW(handle, ct.byref(entry))
        while found:
            if entry.name.lower() in BGI_EXES:
                return True
            found = kernel.Process32NextW(handle, ct.byref(entry))
        return False
    finally:
        kernel.CloseHandle(handle)


class Interrupted(RuntimeError):
    pass


class HeldInput:
    def __init__(self):
        self.keys = []
        self.buttons = []

    def set(self, keys, buttons):
        for button in reversed(self.buttons.copy()):
            if button not in buttons:
                input_api.mouseUp(button=button, _pause=False)
                self.buttons.remove(button)
        for key in reversed(self.keys.copy()):
            if key not in keys:
                input_api.keyUp(key, _pause=False)
                self.keys.remove(key)
        for key in keys:
            if key not in self.keys:
                if input_api.keyDown(key, _pause=False) is False:
                    raise RuntimeError(f"按下 {key} 失败")
                self.keys.append(key)
        for button in buttons:
            if button not in self.buttons:
                if input_api.mouseDown(button=button, _pause=False) is False:
                    raise RuntimeError(f"按下鼠标 {button} 失败")
                self.buttons.append(button)

    def release(self):
        self.set((), ())


def bundled_engine():
    project = Path(__file__).resolve().parents[1]
    manifest = json.loads((project / "bundles" / "manifest.json").read_text(encoding="utf-8"))
    component = manifest["components"]["bettergi"]
    folder = f"bettergi-{component['version']}-{component['sha256'][:12]}"
    return project / ".runtime" / folder / "BetterGI"


class GameControl:
    def __init__(self, state_path, game_exe, bettergi_root=None, namespace="GenshinCodex"):
        self.templates = state_path / "templates"
        self.templates.mkdir(parents=True, exist_ok=True)
        self.game_exe = os.path.normcase(os.path.abspath(game_exe)) if game_exe else None
        self.owner = kernel.CreateMutexW(None, False, f"Local\\{namespace}.InputOwner.v1")
        self.stop_event = kernel.CreateEventW(None, True, False, f"Local\\{namespace}.Stop.v1")
        self.bgi_lease = kernel.CreateEventW(None, True, False, f"Local\\{namespace}.BetterGIOwner.v1")
        if not self.owner or not self.stop_event or not self.bgi_lease:
            raise ct.WinError(ct.get_last_error())
        self.exiting = threading.Event()
        self.capture_device = None
        self.dx_camera = None
        self.dx_failure = False
        self.capture_source = "mss"
        self.owns_bgi_lease = False
        self.bettergi_root = bettergi_root or bundled_engine()
        self.vision = None
        self.checked_bgi = 0.0

    def stop(self):
        if not kernel.SetEvent(self.stop_event):
            raise ct.WinError(ct.get_last_error())
        return {"stopped": True, "message": "急停已锁定，执行中的输入会松开"}

    def stopped(self):
        return kernel.WaitForSingleObject(self.stop_event, 0) == 0

    def bettergi_claimed(self):
        return kernel.WaitForSingleObject(self.bgi_lease, 0) == 0

    def claim_bettergi(self):
        with self.exclusive():
            if self.stopped():
                raise Interrupted("已经急停，无法交接控制")
            if self.bettergi_claimed() and not self.owns_bgi_lease:
                raise Interrupted("另一条连接已经持有 BetterGI 控制权")
            if not kernel.SetEvent(self.bgi_lease):
                raise ct.WinError(ct.get_last_error())
            self.owns_bgi_lease = True
        return {"owner": "bettergi"}

    def release_bettergi(self):
        with self.exclusive():
            if bettergi_running():
                raise Interrupted("BetterGI 仍在运行")
            if not kernel.ResetEvent(self.bgi_lease):
                raise ct.WinError(ct.get_last_error())
            self.owns_bgi_lease = False
        return {"owner": "available"}

    @contextmanager
    def exclusive(self):
        acquired = kernel.WaitForSingleObject(self.owner, 0)
        if acquired == 0x80:
            self.stop()
            kernel.ReleaseMutex(self.owner)
            raise Interrupted("上一个控制进程异常退出，执行急停检查后再恢复")
        if acquired != 0:
            raise Interrupted("已有一个输入序列正在控制游戏")
        try:
            yield
        finally:
            kernel.ReleaseMutex(self.owner)

    def foreground(self):
        window = user.GetForegroundWindow()
        pid = win.DWORD()
        if window:
            user.GetWindowThreadProcessId(window, ct.byref(pid))
        if not pid.value:
            return None
        image_path = game_image_path(pid.value)
        name = os.path.basename(image_path).lower()
        if name not in GAME_EXES or (self.game_exe and os.path.normcase(image_path) != self.game_exe):
            return None
        rect, origin = win.RECT(), win.POINT()
        if not user.GetClientRect(window, ct.byref(rect)) or not user.ClientToScreen(window, ct.byref(origin)):
            return None
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width < 64 or height < 64:
            return None
        return {"handle": window, "pid": pid.value, "left": origin.x, "top": origin.y,
                "width": width, "height": height, "exe": name}

    def require_game(self):
        frame_info = self.foreground()
        if not frame_info:
            raise Interrupted("原神需要位于当前交互桌面的前台")
        return frame_info

    def check_control(self, game):
        if self.exiting.is_set():
            raise Interrupted("控制连接已关闭")
        if all(user.GetAsyncKeyState(key) & 0x8000 for key in INTERRUPT_KEYS):
            self.stop()
        if self.stopped():
            raise Interrupted("急停已锁定")
        if self.bettergi_claimed():
            raise Interrupted("控制权已交给 BetterGI")
        window = user.GetForegroundWindow()
        pid = win.DWORD()
        if not window or window != game["handle"]:
            raise Interrupted("游戏离开前台")
        user.GetWindowThreadProcessId(window, ct.byref(pid))
        if pid.value != game["pid"]:
            raise Interrupted("游戏进程发生变化")
        now = time.monotonic()
        if now - self.checked_bgi >= 0.2:
            self.checked_bgi = now
            if bettergi_running():
                raise Interrupted("BetterGI 主程序占用游戏控制，状态查询和取帧仍可用")

    def resume(self):
        with self.exclusive():
            if all(user.GetAsyncKeyState(key) & 0x8000 for key in INTERRUPT_KEYS):
                raise Interrupted("请先松开急停键")
            if not kernel.ResetEvent(self.stop_event):
                raise ct.WinError(ct.get_last_error())
        return {"stopped": False}

    def status(self):
        owner_pid = win.DWORD()
        window = user.GetForegroundWindow()
        if window:
            user.GetWindowThreadProcessId(window, ct.byref(owner_pid))
        front_is_bettergi = bool(owner_pid.value) and os.path.basename(game_image_path(owner_pid.value)).lower() in BGI_EXES
        return {"game": self.foreground(), "stopped": self.stopped(),
                "bettergi_running": bettergi_running(), "bettergi_foreground": front_is_bettergi,
                "bettergi_lease": self.bettergi_claimed(),
                "emergency_hotkey": "Ctrl+Alt+F10"}

    def close_capture(self):
        if self.dx_camera is not None:
            self.dx_camera.release()
            self.dx_camera = None
        if self.capture_device is not None:
            self.capture_device.close()
            self.capture_device = None

    def close_handles(self):
        for handle in (self.owner, self.stop_event, self.bgi_lease):
            kernel.CloseHandle(handle)

    def grab_dxgi(self, source):
        if self.dx_failure or source["left"] < 0 or source["top"] < 0:
            return None
        right = source["left"] + source["width"]
        bottom = source["top"] + source["height"]
        if self.dx_camera is None:
            try:
                import dxcam

                info = dxcam.output_info()
                match = re.search(
                    r"Device\[(\d+)\] Output\[(\d+)\]: Res:\((\d+),\s*(\d+)\) Rot:0 Primary:True", info,
                )
                if not match:
                    self.dx_failure = True
                    return None
                device, output, width, height = map(int, match.groups())
                self.dx_size = (width, height)
                self.dx_camera = dxcam.create(device_idx=device, output_idx=output, output_color="BGR")
            except Exception:
                self.dx_failure = True
                return None
        if right > self.dx_size[0] or bottom > self.dx_size[1]:
            return None
        try:
            return self.dx_camera.grab(region=(source["left"], source["top"], right, bottom),
                                       new_frame_only=False)
        except Exception:
            self.close_capture()
            self.dx_failure = True
            return None

    def grab(self, game, region=FULL_REGION):
        # 可见区域只从已核对的同一个前台窗口读取。
        if user.GetForegroundWindow() != game["handle"]:
            raise Interrupted("游戏离开前台，已停止取帧")
        rect, origin = win.RECT(), win.POINT()
        if (not user.GetClientRect(game["handle"], ct.byref(rect)) or
                not user.ClientToScreen(game["handle"], ct.byref(origin)) or
                (origin.x, origin.y, rect.right - rect.left, rect.bottom - rect.top) !=
                (game["left"], game["top"], game["width"], game["height"])):
            raise Interrupted("原神窗口的位置或尺寸已变化")
        left, top, width, height = normalized_region(region)
        crop_left = round(left * game["width"])
        crop_top = round(top * game["height"])
        crop_width = round((left + width) * game["width"]) - crop_left
        crop_height = round((top + height) * game["height"]) - crop_top
        if crop_width < 8 or crop_height < 8:
            raise ValueError("区域至少需要 8×8 像素")
        source = {"left": game["left"] + crop_left, "top": game["top"] + crop_top,
                  "width": crop_width, "height": crop_height}
        started = time.perf_counter()
        frame = self.grab_dxgi(source)
        self.capture_source = "dxgi" if frame is not None else "mss"
        if frame is None:
            if self.capture_device is None:
                self.capture_device = mss.MSS()
            screenshot = self.capture_device.grab(source)
            frame = np.asarray(screenshot)[:, :, :3].copy()
        if user.GetForegroundWindow() != game["handle"]:
            raise Interrupted("取帧期间游戏离开前台")
        return frame, round((time.perf_counter() - started) * 1000, 1)

    def frame(self, region=FULL_REGION, max_width=1080, quality=72):
        import base64

        max_width = bounded(max_width, 160, 1920, "max_width", integer=True)
        quality = bounded(quality, 40, 90, "quality", integer=True)
        game = self.require_game()
        frame, capture_ms = self.grab(game, region)
        height, width = frame.shape[:2]
        if width > max_width:
            frame = cv2.resize(frame, (max_width, round(height * max_width / width)),
                               interpolation=cv2.INTER_AREA)
        ok, data = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise RuntimeError("JPEG 编码失败")
        return {"game": game, "region": region, "size": list(reversed(frame.shape[:2])),
                "capture_ms": capture_ms, "capture_source": self.capture_source, "bytes": int(data.size),
                "image": base64.b64encode(data).decode("ascii")}

    def analyze_frame(self, frame, detail=True, allow_unclassified_targets=False, target_kind=None):
        if self.vision is None:
            self.vision = WorldVision(self.bettergi_root)
        return self.vision.analyze(frame, detail=detail, allow_unclassified_targets=allow_unclassified_targets,
                                   target_kind=target_kind)

    def observe(self, samples=1, interval_ms=90):
        samples = bounded(samples, 1, 20, "samples", integer=True)
        interval_ms = bounded(interval_ms, 20, 500, "interval_ms", integer=True)
        game = self.require_game()
        observations = []
        for index in range(samples):
            before = time.perf_counter()
            frame, capture_ms = self.grab(game)
            state, _, _ = self.analyze_frame(frame)
            elapsed = round((time.perf_counter() - before) * 1000, 1)
            observations.append({**state, "capture_ms": capture_ms, "loop_ms": elapsed,
                                 "capture_source": self.capture_source})
            if index + 1 < samples:
                self.exiting.wait(max(0, (interval_ms - elapsed) / 1000))
        return {"latest": observations[-1], "samples": observations,
                "median_loop_ms": round(float(np.median([item["loop_ms"] for item in observations])), 1)}

    def run_local(self, job_id, phases, emit):
        game = self.require_game()
        phases = prepare_phases(phases, self.template_path, game["height"], bounded, normalized_region)
        with self.exclusive():
            self.checked_bgi = 0.0
            self.check_control(game)
            return run_job(self, game, phases, job_id, emit, HeldInput())

    def pause(self, game, until):
        while True:
            self.check_control(game)
            remaining = until - time.perf_counter()
            if remaining <= 0:
                return
            self.exiting.wait(min(0.02, remaining))

    def apply_step(self, game, step, held):
        keys, buttons, duration, delta, pointer = step
        # 转镜分段插值，可与按住移动、瞄准或攀爬同时进行。
        self.check_control(game)
        if pointer and not user.SetCursorPos(
            game["left"] + round(pointer[0] * (game["width"] - 1)),
            game["top"] + round(pointer[1] * (game["height"] - 1)),
        ):
            raise ct.WinError(ct.get_last_error())
        held.set(keys, buttons)
        start = time.perf_counter()
        smooth_steps = min(32, max(1, duration // 16)) if any(delta) else 1
        x_sent, y_sent = 0, 0
        for index in range(1, smooth_steps + 1):
            self.check_control(game)
            x = round(delta[0] * index / smooth_steps)
            y = round(delta[1] * index / smooth_steps)
            if (x, y) != (x_sent, y_sent):
                input_api.moveRel(x - x_sent, y - y_sent, relative=True, _pause=False)
                x_sent, y_sent = x, y
            self.pause(game, start + duration / 1000 * index / smooth_steps)

    def prepare_steps(self, steps):
        if not isinstance(steps, list) or not 1 <= len(steps) <= 80:
            raise ValueError("steps 至少需要 1 步，最多 80 步")
        prepared = []
        for step in steps:
            if not isinstance(step, dict) or not set(step) <= {"keys", "buttons", "ms", "dx", "dy", "pointer"}:
                raise ValueError("输入步骤字段不符合工具定义")
            keys, buttons = step.get("keys", []), step.get("buttons", [])
            if not isinstance(keys, list) or len(keys) > 8 or any(
                not isinstance(key, str) or key not in ALLOWED_KEYS or key not in input_api.KEYBOARD_MAPPING for key in keys
            ) or len(keys) != len(set(keys)):
                raise ValueError("按键无效、重复或超过 8 个")
            if not isinstance(buttons, list) or len(buttons) > 3 or any(
                not isinstance(button, str) or button not in BUTTONS for button in buttons
            ) or len(buttons) != len(set(buttons)):
                raise ValueError("鼠标按键无效或重复")
            duration = bounded(step.get("ms", 70), 1, 10000, "ms", integer=True)
            delta = [bounded(step.get(axis, 0), -2400, 2400, axis, integer=True) for axis in ("dx", "dy")]
            pointer = step.get("pointer")
            if pointer is not None:
                if not isinstance(pointer, list) or len(pointer) != 2 or any(delta):
                    raise ValueError("pointer 需要两个比例，且不能同一段同时转镜")
                pointer = [bounded(value, 0, 1, "鼠标指针坐标") for value in pointer]
            prepared.append((keys, buttons, duration, delta, pointer))
        if sum(step[2] for step in prepared) > 30000:
            raise ValueError("单次序列最多 30 秒")
        return prepared

    def act(self, steps, measure=False):
        prepared = self.prepare_steps(steps)
        game = self.require_game()
        started = time.perf_counter()
        result = {"completed_steps": 0, "planned_steps": len(prepared)}
        with self.exclusive():
            self.checked_bgi = 0.0
            self.check_control(game)
            before = self.grab(game)[0] if measure else None
            held = HeldInput()
            try:
                for step in prepared:
                    self.apply_step(game, step, held)
                    result["completed_steps"] += 1
            except Interrupted as error:
                result["interrupted"] = str(error)
            finally:
                held.release()
            if measure and not result.get("interrupted"):
                self.pause(game, time.perf_counter() + 0.085)
                after, capture_ms = self.grab(game)
                before_gray = cv2.cvtColor(cv2.resize(before, (320, 180)), cv2.COLOR_BGR2GRAY)
                after_gray = cv2.cvtColor(cv2.resize(after, (320, 180)), cv2.COLOR_BGR2GRAY)
                change = cv2.absdiff(before_gray, after_gray)
                result["visual_change"] = {"fraction_over_15": round(float((change > 15).mean()), 4),
                                           "mean_intensity": round(float(change.mean()), 2),
                                           "capture_ms": capture_ms}
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        return result

    def template_path(self, name):
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", name):
            raise ValueError("模板名称限字母、数字、下划线和减号，最长 48 个字符")
        return self.templates / (name + ".png")

    def save_template(self, name, region, overwrite=False):
        path = self.template_path(name)
        image, capture_ms = self.grab(self.require_game(), region)
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        if not 12 <= gray.shape[0] <= 480 or not 12 <= gray.shape[1] <= 480 or gray.std() < 3:
            raise ValueError("选区需要有可辨识纹理，尺寸在 12×12 至 480×480 像素之间")
        ok, data = cv2.imencode(".png", gray)
        if not ok:
            raise RuntimeError("模板 PNG 编码失败")
        with path.open("wb" if overwrite else "xb") as out:
            out.write(data.tobytes())
        return {"name": name, "size": list(reversed(gray.shape)), "capture_ms": capture_ms}

    def track(self, name, region=FULL_REGION, mode="observe", confidence=0.8, duration_ms=1000,
              tolerance=0.035, target=(0.5, 0.5), stop_width=0.07, aim_button=None):
        if mode not in ("observe", "wait", "aim", "follow"):
            raise ValueError("未知模板跟踪模式")
        if aim_button not in (None, "left", "right"):
            raise ValueError("瞄准按键只支持鼠标左键和右键")
        confidence = bounded(confidence, 0.55, 0.99, "confidence")
        duration_ms = bounded(duration_ms, 80, 12000, "duration_ms", integer=True)
        tolerance = bounded(tolerance, 0.01, 0.2, "tolerance")
        stop_width = bounded(stop_width, 0.015, 0.5, "stop_width")
        if not isinstance(target, (list, tuple)) or len(target) != 2:
            raise ValueError("target 需要两个画面比例")
        target = [bounded(value, 0.1, 0.9, "target") for value in target]
        region = normalized_region(region)
        template = cv2.imdecode(np.fromfile(self.template_path(name), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if template is None:
            raise ValueError("模板无法解码")
        game = self.require_game()
        controlling = mode in ("aim", "follow")
        with self.exclusive():
            self.checked_bgi = 0.0
            if controlling:
                self.check_control(game)
            held = HeldInput()
            try:
                return self.track_frames(game, template, region, mode, confidence,
                                         duration_ms, tolerance, target, stop_width, aim_button, held)
            finally:
                held.release()

    def track_frames(self, game, template, region, mode, confidence, duration_ms, tolerance, target,
                     stop_width, aim_button, held):
        started = time.perf_counter()
        deadline = started + duration_ms / 1000
        templates = resize_templates(template, (0.75, 1, 1.25, 1.55))
        samples = 0
        missed = 0
        result = {"status": "timeout"}
        while time.perf_counter() < deadline:
            if self.stopped() or self.exiting.is_set():
                raise Interrupted("已停止模板跟踪")
            if mode in ("aim", "follow"):
                self.check_control(game)
            image, capture_ms = self.grab(game, region)
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            best = peak_template(gray, templates)
            samples += 1
            if best is None:
                raise ValueError("搜索区域小于模板")
            score = best["confidence"]
            x = region[0] + best["center"][0] * gray.shape[1] / game["width"]
            y = region[1] + best["center"][1] * gray.shape[0] / game["height"]
            relative_width = best["size"][0] * gray.shape[1] / game["width"]
            found = score >= confidence
            result.update(found=found, score=round(score, 3), center=[round(x, 4), round(y, 4)],
                          relative_width=round(relative_width, 4), capture_ms=capture_ms)
            if mode == "observe" or (mode == "wait" and found):
                result["status"] = "found" if found else "missing"
                break
            if not found:
                held.release()
                missed += 1
                if mode in ("aim", "follow") and missed >= 3:
                    result["status"] = "target_lost"
                    break
                self.exiting.wait(0.045)
                continue
            missed = 0
            dx, dy = x - target[0], y - target[1]
            if mode in ("aim", "follow"):
                aligned = abs(dx) < tolerance and (mode == "follow" or abs(dy) < tolerance)
                if aligned and (mode == "aim" or relative_width >= stop_width):
                    result["status"] = "aligned" if mode == "aim" else "near_target"
                    break
                movement = ["w"] if mode == "follow" and abs(dx) < max(tolerance, 0.12) else []
                turn_x = max(-90, min(90, round(dx * 300)))
                turn_y = max(-60, min(60, round(dy * 245))) if mode == "aim" else 0
                ms = max(1, min(70, round((deadline - time.perf_counter()) * 1000)))
                buttons = [aim_button] if aim_button else []
                self.apply_step(game, (movement, buttons, ms, (turn_x, turn_y), None), held)
            else:
                self.exiting.wait(0.05)
        result.update(samples=samples, elapsed_ms=round((time.perf_counter() - started) * 1000))
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--game-exe")
    parser.add_argument("--bettergi-root", type=Path)
    settings = parser.parse_args()
    control = GameControl(settings.state, settings.game_exe, settings.bettergi_root)
    methods = {"status": control.status, "frame": control.frame, "act": control.act,
               "save_template": control.save_template, "track": control.track,
               "resume": control.resume, "stop": control.stop, "observe": control.observe,
               "claim_bettergi": control.claim_bettergi, "release_bettergi": control.release_bettergi}
    executor = ThreadPoolExecutor(max_workers=1)
    write_lock = threading.Lock()
    busy = threading.BoundedSemaphore(1)

    def send(message):
        with write_lock:
            print(json.dumps(message, ensure_ascii=False, allow_nan=False), flush=True)

    def progress(job_id, value):
        send({"event": "job_progress", "job_id": job_id, "value": value})

    methods["run_local"] = lambda job_id, phases: control.run_local(job_id, phases, progress)

    def reply(request):
        request_id = request.get("id")
        try:
            result = methods[request["method"]](**request.get("args", {}))
            response = {"id": request_id, "result": result}
        except Interrupted as error:
            response = {"id": request_id, "result": {"status": "interrupted", "reason": str(error)}} if (
                request.get("method") == "run_local"
            ) else {"id": request_id, "error": str(error), "type": type(error).__name__}
        except Exception as error:
            response = {"id": request_id, "error": str(error), "type": type(error).__name__}
        send(response)

    def run(request):
        try:
            reply(request)
        finally:
            busy.release()

    try:
        for line in sys.stdin:
            try:
                request = json.loads(line)
            except ValueError:
                continue
            if not isinstance(request, dict):
                continue
            if request.get("method") == "stop":
                reply(request)
            elif busy.acquire(blocking=False):
                executor.submit(run, request)
            else:
                send({"id": request.get("id"), "error": "当前有操作正在运行"})
    finally:
        control.exiting.set()
        executor.submit(control.close_capture)
        executor.shutdown(wait=True)
        control.close_handles()


if __name__ == "__main__":
    main()
