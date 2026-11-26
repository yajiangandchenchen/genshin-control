// SPDX-License-Identifier: GPL-3.0-only OR MIT
import { existsSync, mkdirSync, readFileSync, renameSync, rmdirSync, writeFileSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { execFile, spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { setTimeout as delay } from "node:timers/promises";
import { promisify } from "node:util";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { paths, localRoot, prepare, settings } from "../scripts/runtime.mjs";
import { compatibilityLogs, compatibilityMcp, engineDiagnostic } from "./diagnostics.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const runtime = paths.engine;
const skill = paths.controls;
const legacyEntry = paths.legacy;
const python = paths.python;
const controllerPath = path.join(here, "game_control.py");
const statePath = paths.state;
const sceneName = "CodexScene";
const scenePath = path.join(runtime, "User", "JsScript", sceneName);
const uiScript = path.join(here, "bettergi_ui.ps1");
const runFile = promisify(execFile);
const protocolVersions = new Set(["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]);
const log = (message) => process.stderr.write("[原神控制] " + message + "\n");

class ChildRpc {
  constructor(command, args, extraEnv, label, onEvent) {
    this.label = label;
    this.nextId = 0;
    this.pending = new Map();
    this.closed = false;
    this.child = spawn(command, args, {
      windowsHide: true,
      env: { ...process.env, ...extraEnv },
      stdio: ["pipe", "pipe", "pipe"],
    });
    const fromChild = createInterface({ input: this.child.stdout });
    fromChild.on("line", (line) => {
      try {
        const response = JSON.parse(line);
        if (response.event) {
          onEvent?.(response);
          return;
        }
        const request = this.pending.get(response.id);
        if (!request) return;
        this.pending.delete(response.id);
        clearTimeout(request.timer);
        if (response.error) {
          request.reject(new Error(response.error.message || response.error));
        } else {
          request.resolve(response.result);
        }
      } catch {
        log(this.label + " 返回了无法解析的 JSON");
      }
    });
    this.child.stderr.on("data", (data) => process.stderr.write(data));
    this.child.stdin.on("error", (error) => this.disconnect(error));
    this.child.on("error", (error) => this.disconnect(error));
    this.child.on("close", (code) => this.disconnect(new Error(this.label + " 已退出，退出码 " + code)));
  }

  disconnect(error) {
    if (this.closed) return;
    this.closed = true;
    for (const request of this.pending.values()) {
      clearTimeout(request.timer);
      request.reject(error);
    }
    this.pending.clear();
    this.child.stdin.end();
  }

  request(message, timeoutMs = 48000) {
    if (this.closed) return Promise.reject(new Error(this.label + " 连接已关闭"));
    const id = ++this.nextId;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(this.label + " 请求超时"));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.child.stdin.write(JSON.stringify({ id, ...message }) + "\n");
    });
  }

  notify(message) {
    if (!this.closed) this.child.stdin.write(JSON.stringify(message) + "\n");
  }
}

let controller;
const localJobs = new Map();
let activeLocalJob;

function onWorkerEvent(packet) {
  if (packet.event !== "job_progress") return;
  const job = localJobs.get(packet.job_id);
  if (job && job.state === "running") job.latest = packet.value;
}

function workerCall(method, args = {}, timeoutMs) {
  if (!controller || controller.closed) {
    const launchArgs = ["-X", "utf8", "-B", "-u", controllerPath, "--state", statePath, "--bettergi-root", runtime];
    const game = settings().game_exe;
    if (game) launchArgs.push("--game-exe", game);
    controller = new ChildRpc(python, launchArgs, { PYTHONUTF8: "1" }, "实时控制", onWorkerEvent);
  }
  return controller.request({ method, args }, timeoutMs);
}

function startLocalJob(args) {
  if (activeLocalJob) throw new Error("已有本地闭环任务在运行");
  if (!Array.isArray(args.phases) || args.phases.length < 1 || args.phases.length > 12) {
    throw new Error("phases 需要 1 至 12 个阶段");
  }
  const id = randomUUID();
  const job = { id, state: "running", started_at: Date.now() };
  if (localJobs.size >= 12) localJobs.delete(localJobs.keys().next().value);
  localJobs.set(id, job);
  activeLocalJob = id;
  workerCall("run_local", { job_id: id, phases: args.phases }, 127000).then((result) => {
    job.state = result.status;
    job.result = result;
  }).catch((error) => {
    job.state = "error";
    job.error = error.message;
  }).finally(() => {
    job.finished_at = Date.now();
    if (activeLocalJob === id) activeLocalJob = undefined;
  });
  return { id, state: job.state };
}

let legacy;
let legacyInit;
async function legacyCall(method, params = {}) {
  if (!legacy || legacy.closed) {
    const game = settings().game_exe;
    legacy = new ChildRpc(paths.node, [legacyEntry], {
      BETTERGI_ROOT: runtime,
      BETTERGI_SKILL_DIR: skill,
      BETTERGI_MCP: paths.entry,
      GENSHIN_CONTROL_POWERSHELL: paths.powershell,
      GENSHIN_CONTROL_STATE: statePath,
      GENSHIN_CONTROL_CACHE: path.join(localRoot, "cache"),
      ...(game ? { GENSHIN_EXE: game } : {}),
    }, "BetterGI 原有 MCP");
    const client = legacy;
    legacyInit = client.request({
      jsonrpc: "2.0", method: "initialize",
      params: { protocolVersion: "2024-11-05", capabilities: {}, clientInfo: { name: "genshin-control", version: "1.0.0" } },
    }).then(() => client.notify({ jsonrpc: "2.0", method: "notifications/initialized" }));
  }
  await legacyInit;
  return legacy.request({ jsonrpc: "2.0", method, params }, 55000);
}

function nativeSettings() {
  const configPath = path.join(runtime, "User", "config.json");
  if (!existsSync(configPath)) return null;
  const config = JSON.parse(readFileSync(configPath, "utf8").replace(/^\uFEFF/, ""));
  const access = config.externalAccessConfig ?? config.ExternalAccessConfig;
  if (!access) return null;
  if (!Number.isInteger(access.port) || access.port < 1024 || access.port > 65535) {
    throw new Error("BetterGI 外部访问端口未配置");
  }
  return access;
}

async function readMcpResponse(response) {
  const type = response.headers.get("content-type") ?? "";
  if (type.includes("application/json")) return response.json();
  if (!type.includes("text/event-stream") || !response.body) throw new Error("BetterGI 返回了未知的 MCP 响应格式");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      if (buffer.length > 4_000_000) throw new Error("BetterGI 响应超过等待缓冲区");
      let newline;
      while ((newline = buffer.indexOf("\n")) !== -1) {
        const line = buffer.slice(0, newline).trimEnd();
        buffer = buffer.slice(newline + 1);
        if (!line.startsWith("data:")) continue;
        const data = JSON.parse(line.slice(5).trim());
        if (data.id !== undefined) return data;
      }
    }
  } finally {
    await reader.cancel();
  }
  throw new Error("BetterGI 已结束 MCP 响应");
}

async function nativeMcp(method, params) {
  const config = nativeSettings();
  if (!config?.mcpEnabled) return compatibilityMcp(method, params);
  if (!config.accessToken) throw new Error("BetterGI 原生 MCP 缺少本地访问令牌");
  const url = "http://127.0.0.1:" + config.port + "/mcp";
  const headers = {
    "Authorization": "Bearer " + config.accessToken,
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
  };
  let sessionId;
  async function post(body) {
    const response = await fetch(url, { method: "POST", headers, body: JSON.stringify(body),
                                        redirect: "error", signal: AbortSignal.timeout(6000) });
    if (!response.ok) throw new Error("BetterGI MCP 返回 HTTP " + response.status);
    sessionId = response.headers.get("mcp-session-id") || sessionId;
    if (body.id === undefined) {
      await response.body?.cancel();
      return;
    }
    const data = await readMcpResponse(response);
    if (data.id !== body.id) throw new Error("BetterGI MCP 的响应编号不匹配");
    if (data.error) throw new Error(data.error.message || "BetterGI MCP 调用错误");
    return data.result;
  }
  try {
    const init = await post({
      jsonrpc: "2.0", id: 1, method: "initialize",
      params: { protocolVersion: "2025-06-18", capabilities: {},
                clientInfo: { name: "genshin-control", version: "1.0.0" } },
    });
    if (!protocolVersions.has(init.protocolVersion)) throw new Error("BetterGI MCP 协议版本不受支持");
    headers["MCP-Protocol-Version"] = init.protocolVersion;
    if (sessionId) headers["Mcp-Session-Id"] = sessionId;
    await post({ jsonrpc: "2.0", method: "notifications/initialized" });
    return await post({ jsonrpc: "2.0", id: 2, method, params });
  } finally {
    if (sessionId) {
      const response = await fetch(url, { method: "DELETE", headers, redirect: "error",
                                          signal: AbortSignal.timeout(1200) }).catch(() => null);
      await response?.body?.cancel();
    }
  }
}

async function nativeHttp(kind) {
  if (kind !== "health" && kind !== "version") throw new Error("只支持 health 和 version");
  const config = nativeSettings();
  if (!config?.httpApiEnabled) return engineDiagnostic(kind);
  if (!config.accessToken) throw new Error("BetterGI 原生 HTTP 缺少本地访问令牌");
  const response = await fetch("http://127.0.0.1:" + config.port + "/api/v1/" + kind, {
    headers: { Authorization: "Bearer " + config.accessToken },
    signal: AbortSignal.timeout(5000), redirect: "error",
  });
  if (!response.ok) throw new Error("BetterGI 本机 API 返回 HTTP " + response.status);
  return response.json();
}

async function nativeLogs({ duration_ms = 1200, limit = 40, minimum_level = "Information" }) {
  if (!Number.isInteger(duration_ms) || duration_ms < 100 || duration_ms > 5000 ||
      !Number.isInteger(limit) || limit < 1 || limit > 100 ||
      !["Verbose", "Debug", "Information", "Warning", "Error", "Fatal"].includes(minimum_level)) {
    throw new Error("实时日志参数无效");
  }
  const config = nativeSettings();
  if (!config?.webSocketEnabled) return compatibilityLogs(duration_ms, limit, minimum_level);
  const url = new URL("ws://127.0.0.1:" + config.port + "/ws/v1/logs");
  url.searchParams.set("minimumLevel", minimum_level);
  const socket = new WebSocket(url);
  const records = [];
  let opened = false;
  let failed = false;
  let done;
  const limited = new Promise((resolve) => { done = resolve; });
  socket.addEventListener("open", () => { opened = true; });
  socket.addEventListener("error", () => { failed = true; done(); });
  socket.addEventListener("close", () => done());
  socket.addEventListener("message", (event) => {
    if (records.length >= limit) return;
    try {
      records.push(JSON.parse(event.data));
    } catch {
      records.push({ message: String(event.data).slice(0, 2000) });
    }
    if (records.length >= limit) done();
  });
  await Promise.race([limited, delay(duration_ms, undefined, { ref: false })]);
  socket.close();
  if (!opened && failed) throw new Error("BetterGI 的本机日志连接失败");
  return { opened, records };
}

function readSceneFile(name) {
  try {
    const data = readFileSync(path.join(scenePath, name), "utf8");
    return JSON.parse(data);
  } catch (error) {
    if (error.code === "ENOENT" || error instanceof SyntaxError) return null;
    throw error;
  }
}

function sceneStatus(id) {
  const request = readSceneFile("request.json");
  const targetId = id ?? request?.id;
  const result = readSceneFile("result.json");
  if (result && (!targetId || result.id === targetId)) {
    const latest = result.state === "running" ? readSceneFile("telemetry.json") : null;
    return latest?.id === targetId ? { ...result, latest } : result;
  }
  if (request && request.id === targetId) {
    return { id: request.id, operation: request.operation, state: "submitted" };
  }
  return { state: "not_found", id };
}

function sceneString(value, label, maximum = 180) {
  if (typeof value !== "string" || !value.trim() || value.length > maximum) {
    throw new Error(label + "长度或内容无效");
  }
  return value.trim();
}

function sceneInteger(value, label, min, max) {
  if (!Number.isInteger(value) || value < min || value > max) {
    throw new Error(label + "超出允许范围");
  }
  return value;
}

function sceneArgs(operation, args) {
  const mapName = () => args.map ? sceneString(args.map, "地图名", 60) : "Teyvat";
  switch (operation) {
    case "probe":
      return {};
    case "observe":
      return { map: mapName(), position: args.position !== false, quest: args.quest === true, party: args.party === true,
               samples: sceneInteger(args.samples ?? 5, "采样次数", 1, 24),
               interval_ms: sceneInteger(args.interval_ms ?? 200, "采样间隔", 80, 1000),
               cache_ms: sceneInteger(args.cache_ms ?? 300, "坐标缓存时长", 0, 2000) };
    case "combat_script":
      return { script: sceneString(args.script, "战斗策略", 4000),
               avatar: args.avatar ? sceneString(args.avatar, "角色名", 30) : null };
    case "auto_fight": {
      const strategy = sceneString(args.strategy, "已安装的策略名");
      if (/[\\/:]/.test(strategy) || strategy === "." || strategy === "..") {
        throw new Error("战斗策略应使用已安装的策略名");
      }
      return { strategy, timeout_seconds: sceneInteger(args.timeout_seconds ?? 90, "战斗超时", 5, 180),
               loot: args.loot === true };
    }
    case "pathing": {
      const route = sceneString(args.route, "已安装的路线");
      if (path.win32.isAbsolute(route) || route.includes(":") ||
          route.split(/[\\/]/).some((part) => !part || part === "." || part === "..") ||
          !route.toLowerCase().endsWith(".json")) throw new Error("路线需要使用已安装的相对 JSON 路径");
      return { route };
    }
    case "navigate": {
      const modes = ["walk", "run", "dash", "climb", "fly", "jump", "swim"];
      if (!Array.isArray(args.points) || args.points.length < 1 || args.points.length > 50) {
        throw new Error("路线需要 1 至 50 个途经点");
      }
      const points = args.points.map((point, index) => {
        if (!point || ![point.x, point.y].every((v) => Number.isFinite(v) && Math.abs(v) <= 100000) ||
            !modes.includes(point.mode ?? "walk")) throw new Error("途经点坐标或移动方式无效");
        return { x: point.x, y: point.y, mode: point.mode ?? "walk",
                 type: index === args.points.length - 1 ? "target" : "path" };
      });
      return { map: mapName(), points };
    }
    case "teleport":
      if (![args.x, args.y].every((coordinate) => Number.isFinite(coordinate) && Math.abs(coordinate) <= 100000)) {
        throw new Error("传送需要有效的地图坐标");
      }
      return { x: args.x, y: args.y, map: args.map ? sceneString(args.map, "地图名", 40) : null };
    case "dialogue":
      return { option: sceneString(args.option, "对话选项"),
               skip_times: sceneInteger(args.skip_times ?? 10, "跳过次数", 1, 30), orange: args.orange === true };
    default:
      throw new Error("不支持的场景任务类型");
  }
}

async function startScene({ operation, arguments: args = {} }) {
  if (!existsSync(path.join(scenePath, "manifest.json"))) throw new Error("BetterGI 场景脚本尚未安装");
  const validated = sceneArgs(operation, args);
  const ownerPath = path.join(scenePath, ".submission");
  try {
    mkdirSync(ownerPath);
  } catch (error) {
    if (error.code === "EEXIST") throw new Error("另一个场景任务正在提交");
    throw error;
  }
  try {
    const status = await workerCall("status");
    if (!status.game && !status.bettergi_foreground) throw new Error("原神或 BetterGI 需要位于当前交互桌面的前台");
    if (status.stopped) throw new Error("实时控制已急停，请先确认可以恢复");
    const previous = readSceneFile("request.json");
    if (previous && status.bettergi_running && sceneStatus(previous.id).state === "running") {
      throw new Error("上一个 BetterGI 场景任务还在运行");
    }
    if (previous && Date.now() - previous.submitted_at < 12000 && sceneStatus(previous.id).state === "submitted") {
      throw new Error("BetterGI 正在接受上一条任务");
    }
    const group = await legacyCall("tools/call", { name: "bgi_run_js", arguments: { name: sceneName, run: false } });
    if (group.isError) return group;
    const request = { id: randomUUID(), operation, arguments: validated, submitted_at: Date.now() };
    await workerCall("claim_bettergi");
    try {
      const temp = path.join(scenePath, "request.pending.json");
      writeFileSync(temp, JSON.stringify(request), "utf8");
      renameSync(temp, path.join(scenePath, "request.json"));
      const message = await runSceneUi("RunScene");
      return json({ id: request.id, state: "submitted", launch: message });
    } catch (error) {
      try {
        if (sceneStatus(request.id).state === "submitted") {
          writeFileSync(path.join(scenePath, "result.json"),
                        JSON.stringify({ id: request.id, operation, state: "error", result: error.message }), "utf8");
        }
      } finally {
        await workerCall("release_bettergi").catch(() => {});
      }
      throw error;
    }
  } finally {
    rmdirSync(ownerPath);
  }
}

async function runSceneUi(action) {
  const argv = ["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", uiScript, "-BetterGIExe", path.join(runtime, "BetterGI.exe"), "-Action", action];
  const game = settings().game_exe;
  if (game) argv.push("-GameExe", game);
  try {
    const { stdout } = await runFile(paths.powershell, argv, {
      windowsHide: true, timeout: 32000, maxBuffer: 128 * 1024, encoding: "utf8",
    });
    return stdout.trim();
  } catch (error) {
    throw new Error((error.stderr || error.message).trim());
  }
}

const object = (properties = {}, required = []) => ({
  type: "object", properties, required, additionalProperties: false,
});
const text = (description) => ({ type: "string", description });
const bool = (description) => ({ type: "boolean", description });
const integer = (minimum, maximum, description) => ({ type: "integer", minimum, maximum, description });
const number = (minimum, maximum, description) => ({ type: "number", minimum, maximum, description });
const array = (items, minItems, maxItems, description) => ({ type: "array", items, minItems, maxItems, description });
const choice = (...values) => ({ type: "string", enum: values });
const region = array(number(0, 1), 4, 4, "在游戏客户区内的 [左、上、宽、高] 比例");
const point = array(number(0, 1), 2, 2, "在游戏客户区内的 [横、纵] 比例");
const templateName = { type: "string", pattern: "^[A-Za-z0-9_-]{1,48}$" };
const step = object({
  keys: array(text("小写按键名，例如 w、space、shift、e、q、1"), 0, 8),
  buttons: array(choice("left", "right", "middle"), 0, 3),
  ms: integer(1, 10000, "本段维持的毫秒数，默认 70"),
  dx: integer(-2400, 2400, "分段相对横向转镜量"),
  dy: integer(-2400, 2400, "分段相对纵向转镜量"),
  pointer: point,
});
const target = object({
  kind: choice("blue_quest", "gold_quest", "enemy", "template"),
  name: templateName, region,
}, ["kind"]);
const cue = object({ name: templateName, region, confidence: number(.70, .99) }, ["name"]);
const phase = object({
  mode: choice("walk", "run", "dash", "jump", "climb", "glide", "spirit", "aim", "shoot", "combat", "dialogue"),
  target, aim: point, timeout_ms: integer(300, 45000),
  confidence: number(.65, .99), tolerance: number(.01, .12), stop_width: number(.012, .55),
  yaw_gain: number(45, 950), pitch_gain: number(30, 750),
  charge_ms: integer(45, 1450), stall_ms: integer(220, 7000),
  entry_cue: cue, success_cue: cue, failure_cue: cue, jump_retries: integer(0, 2),
  attack_keys: array(choice("1", "2", "3", "4", "e", "q", "space"), 0, 7),
}, ["mode"]);
const tools = [];
const handlers = new Map();
const json = (value) => ({ content: [{ type: "text", text: JSON.stringify(value) }] });
const bad = (message) => ({ isError: true, content: [{ type: "text", text: message }] });
function add(name, description, inputSchema, action, readOnlyHint = false) {
  tools.push({ name, description, inputSchema, annotations: { readOnlyHint } });
  handlers.set(name, action);
}

add("gctl_status", "检查原神前台、急停和 BetterGI 占用状态。", object(), async () => {
  const result = await workerCall("status");
  let native;
  try {
    const config = nativeSettings();
    native = config ? { port: config.port, http: config.httpApiEnabled, mcp: config.mcpEnabled,
                        logs: config.webSocketEnabled } : { configured: false, diagnostics: "portable" };
  } catch {
    native = { configured: false };
  }
  return json({ ...result, bettergi_path: runtime, bettergi_installed: existsSync(path.join(runtime, "BetterGI.exe")), native });
}, true);

add("gctl_frame", "只裁取原神前台画面，输出压缩 JPEG、尺寸、延迟。", object({
  region, max_width: integer(160, 1920), quality: integer(40, 90),
}), async (args) => {
  const { image, ...info } = await workerCall("frame", args);
  return { content: [...json(info).content, { type: "image", data: image, mimeType: "image/jpeg" }] };
}, true);
add("gctl_observe", "在本地识别当前场景、剧情、飞行或攀爬图标、任务标记、敌人血条及小地图方向。返回结构化状态。", object({
  samples: integer(1, 20), interval_ms: integer(20, 500),
}), async (args) => json(await workerCall("observe", args)), true);

add("gctl_act", "定时组合长按、相对转镜、拖动、按键和点击。相邻步骤共享的按键保持按下，结束和中断时释放，总时长最多 30 秒。", object({
  steps: array(step, 1, 80, "每一段同时维持其中按键与鼠标按钮"),
  measure: bool("可选，返回动作前后画面变化指标，不发送图像"),
}, ["steps"]), async (args) => json(await workerCall("act", args)));

add("gctl_save_template", "把当前前台画面的可见地标裁成局部模板，保存在本机。", object({
  name: templateName, region, overwrite: bool("显式允许替换同名模板"),
}, ["name", "region"]), async (args) => json(await workerCall("save_template", args)));

add("gctl_track_template", "在本地多尺度检测模板。observe 查看，wait 等待出现，aim 自动转镜，follow 对准时短步前进。丢失目标后会停。", object({
  name: templateName, region, mode: choice("observe", "wait", "aim", "follow"),
  confidence: number(0.55, 0.99), duration_ms: integer(80, 12000),
  tolerance: number(0.01, 0.2), target: point, stop_width: number(0.015, 0.5),
  aim_button: choice("left", "right"),
}, ["name"]), async (args) => json(await workerCall("track", args)));

add("gctl_stop", "急停并锁定实时控制，当前序列会释放持有的按键。", object(),
    async () => json(await workerCall("stop", {}, 3000)));
add("gctl_resume", "解除实时控制急停锁定。", object(), async () => json(await workerCall("resume")));
add("gctl_job_start", "提交本地闭环阶段。目标丢失、场景未知、卡住、离开前台或达到截止时间时结束，回传阶段和时延。", object({
  phases: array(phase, 1, 12),
}, ["phases"]), async (args) => json(startLocalJob(args)));
add("gctl_job_status", "读取本地闭环作业的当前阶段、目标、结束原因和时延。", object({
  id: text("本地作业编号"),
}), async ({ id }) => json(localJobs.get(id ?? activeLocalJob ?? Array.from(localJobs.keys()).at(-1)) ?? { state: "not_found" }), true);
add("gctl_job_cancel", "中止当前本地闭环作业并松开输入。", object({
  id: text("本地作业编号"),
}, ["id"]), async ({ id }) => {
  if (id !== activeLocalJob) throw new Error("该本地作业当前未运行");
  return json(await workerCall("stop", {}, 3000));
});

add("bgi_native_catalog", "列出引擎诊断工具。启用原生 MCP 时动态读取其清单，便携兼容模式提供健康与版本。", object(),
    async () => json(await nativeMcp("tools/list", {})), true);
add("bgi_native_call", "调用 BetterGI 自带的任意已公布原生 MCP 工具。", object({
  name: text("原生 tools/list 返回的名称"), arguments: { type: "object" },
}, ["name"]), async (args) => nativeMcp("tools/call", { name: args.name, arguments: args.arguments ?? {} }));
add("bgi_native_http", "查询引擎健康或版本。原生 HTTP 未启用时返回带来源的 Windows 进程或文件版本信息。", object({
  kind: choice("health", "version"),
}, ["kind"]), async ({ kind }) => json(await nativeHttp(kind)), true);
add("bgi_native_logs", "短时读取引擎日志。可用时连接原生 WebSocket，便携版从自己的日志文件读取。", object({
  duration_ms: integer(100, 5000), limit: integer(1, 100),
  minimum_level: choice("Verbose", "Debug", "Information", "Warning", "Error", "Fatal"),
}), async (args) => json(await nativeLogs(args)), true);
add("bgi_scene_start", "通过 BetterGI 公开脚本宿主提交观测、战斗、寻路、传送或对话任务。传入任务编号查询进度。", object({
  operation: choice("probe", "observe", "combat_script", "auto_fight", "pathing", "navigate", "teleport", "dialogue"),
  arguments: { type: "object", description: "observe 接受 map、samples、interval_ms、cache_ms、position、quest、party。navigate 传 map 和 points，点含 x、y、mode。combat_script 传 script 和可选 avatar。auto_fight 传 strategy 和可选 timeout_seconds、loot。pathing 传 route。teleport 传 x、y 和可选 map。dialogue 传 option 和可选 skip_times、orange。" },
}, ["operation"]), startScene);
add("bgi_scene_status", "查询最近一个 BetterGI 场景脚本的运行或返回状态。", object({
  id: text("提交任务返回的编号，可省略"),
}), async ({ id }) => json(sceneStatus(id)), true);
add("bgi_scene_release", "关闭这套便携版的 BetterGI，释放原神给实时控制工具。", object(), async () => {
  const result = await runSceneUi("Release");
  const owner = await workerCall("release_bettergi");
  const current = sceneStatus();
  if (current.state === "running" || current.state === "submitted") {
    const finished = { ...current, state: "cancelled" };
    writeFileSync(path.join(scenePath, "result.json"), JSON.stringify(finished), "utf8");
  }
  return json({ result, ...owner });
});

const legacyChanging = new Set([
  "bgi_start_game", "bgi_run_one_dragon", "bgi_run_groups", "bgi_run_task_progress", "bgi_collect_now",
  "bgi_close", "bgi_focus_game", "bgi_run_js", "bgi_collect_material", "bgi_scene_start", "bgi_scene_release",
]);
const legacyTaskStarts = new Set(["bgi_start_game", "bgi_run_one_dragon", "bgi_run_groups", "bgi_run_task_progress", "bgi_collect_now"]);
const inputRequests = new Set();
let legacyChangingCount = 0;
let legacyNames;
async function getLegacyTools() {
  const list = (await legacyCall("tools/list")).tools;
  legacyNames = new Set(list.map((tool) => tool.name));
  return list;
}

async function dispatchTool(requestId, name, args) {
  await prepare(log);
  const isInput = name === "gctl_act" || name === "gctl_job_start" ||
    (name === "gctl_track_template" && ["aim", "follow"].includes(args.mode));
  const replacesInput = legacyChanging.has(name);
  if (isInput && legacyChangingCount) throw new Error("BetterGI 正在接管游戏，请等待操作结束");
  if (replacesInput && (inputRequests.size || activeLocalJob)) throw new Error("实时控制正在执行，请先完成或停止当前操作");
  if (isInput && activeLocalJob) throw new Error("本地闭环作业正在运行");
  if (isInput) inputRequests.add(requestId);
  if (replacesInput) legacyChangingCount++;
  try {
    if (handlers.has(name)) return await handlers.get(name)(args);
    if (!legacyNames) await getLegacyTools();
    if (!legacyNames.has(name)) throw new Error("未知原神控制工具 " + name);
    const startsTask = legacyTaskStarts.has(name) ||
      ((name === "bgi_run_js" || name === "bgi_collect_material") && args.run === true);
    if (startsTask) await workerCall("claim_bettergi");
    try {
      return await legacyCall("tools/call", { name, arguments: args });
    } finally {
      if (startsTask || name === "bgi_close") {
        await workerCall("release_bettergi").catch(() => {});
      }
    }
  } finally {
    if (isInput) inputRequests.delete(requestId);
    if (replacesInput) legacyChangingCount--;
  }
}

const write = (message) => process.stdout.write(JSON.stringify({ jsonrpc: "2.0", ...message }) + "\n");
async function handle({ id, method, params = {} }) {
  if (method === "notifications/cancelled" && inputRequests.has(params.requestId)) {
    await workerCall("stop", {}, 2000).catch((error) => log(error.message));
  }
  if (id === undefined || id === null) return;
  const response = (result) => write({ id, result });
  if (method === "initialize") {
    response({
      protocolVersion: protocolVersions.has(params.protocolVersion) ? params.protocolVersion : "2025-11-25",
      capabilities: { tools: {} },
      serverInfo: { name: "genshin-control", version: "1.2.0" },
    });
  } else if (method === "tools/list") {
    const original = await getLegacyTools();
    response({ tools: [...tools, ...original] });
  } else if (method === "tools/call") {
    try {
      if (typeof params.name !== "string" || (params.arguments && typeof params.arguments !== "object")) {
        throw new Error("工具名称或参数无效");
      }
      response(await dispatchTool(id, params.name, params.arguments ?? {}));
    } catch (error) {
      response(bad(error.message));
    }
  } else if (method === "ping") {
    response({});
  } else if (method === "resources/list") {
    response({ resources: [] });
  } else if (method === "resources/templates/list") {
    response({ resourceTemplates: [] });
  } else if (method === "prompts/list") {
    response({ prompts: [] });
  } else {
    write({ id, error: { code: -32601, message: "未知 MCP 方法 " + method } });
  }
}

const incoming = createInterface({ input: process.stdin });
incoming.on("line", (line) => {
  let request;
  try {
    request = JSON.parse(line);
  } catch {
    write({ id: null, error: { code: -32700, message: "请求不是合法 JSON" } });
    return;
  }
  handle(request).catch((error) => {
    write({ id: request?.id ?? null, error: { code: -32603, message: error.message } });
  });
});
incoming.on("close", () => {
  controller?.disconnect(new Error("控制连接已关闭"));
  legacy?.disconnect(new Error("控制连接已关闭"));
});
