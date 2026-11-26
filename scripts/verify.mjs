// SPDX-License-Identifier: GPL-3.0-only OR MIT
import { execFile, spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { promisify } from "node:util";
import { paths } from "./runtime.mjs";

const exec = promisify(execFile);

export async function verifyServer() {
  const pythonCheck = [
    "import pathlib,sys,importlib.util",
    "base=pathlib.Path(sys.executable).resolve().parent",
    "import cv2,numpy,mss,pydirectinput,comtypes",
    "names=('cv2','numpy','mss','pydirectinput','comtypes','dxcam')",
    "assert all(pathlib.Path(importlib.util.find_spec(n).origin).resolve().is_relative_to(base) for n in names)",
    "print(sys.version.split()[0])",
  ].join("\n");
  const { stdout: py } = await exec(paths.python, ["-X", "utf8", "-B", "-c", pythonCheck], { windowsHide: true });
  const { stdout: psh } = await exec(paths.powershell, [
    "-NoProfile", "-NonInteractive", "-Command",
    "Add-Type -AssemblyName UIAutomationClient -ErrorAction Stop; $PSVersionTable.PSVersion.ToString()",
  ], { windowsHide: true });
  const child = spawn(paths.node, [paths.entry], { windowsHide: true, stdio: ["pipe", "pipe", "pipe"] });
  const waiting = new Map();
  let serial = 0;
  let diagnostics = "";
  child.stderr.on("data", (part) => { diagnostics = (diagnostics + part.toString("utf8")).slice(-1800); });
  function fail(error) {
    for (const pending of waiting.values()) pending.reject(error);
    waiting.clear();
  }
  child.on("error", fail);
  child.on("exit", (code) => fail(new Error("MCP 提前退出 " + code + " " + diagnostics)));
  child.stdin.on("error", fail);
  const reader = createInterface({ input: child.stdout });
  reader.on("line", (line) => {
    try {
      const message = JSON.parse(line);
      const request = waiting.get(message.id);
      if (!request) return;
      waiting.delete(message.id);
      if (message.error) request.reject(new Error(message.error.message));
      else request.resolve(message.result);
    } catch (error) { fail(error); }
  });
  function send(method, params) {
    const id = ++serial;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        waiting.delete(id);
        reject(new Error(method + " 自检超时 " + diagnostics));
      }, 35000);
      waiting.set(id, {
        resolve(value) { clearTimeout(timer); resolve(value); },
        reject(error) { clearTimeout(timer); reject(error); },
      });
      child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
    });
  }
  try {
    const init = await send("initialize", { protocolVersion: "2025-06-18", capabilities: {},
                                           clientInfo: { name: "portable-check", version: "1" } });
    child.stdin.write(JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }) + "\n");
    const tools = (await send("tools/list", {})).tools;
    if (new Set(tools.map((t) => t.name)).size !== 47) throw new Error("MCP 工具清单数量不符");
    for (const [name, args] of [
      ["gctl_status", {}], ["bgi_list_strategies", {}], ["bgi_list_groups", {}], ["bgi_scene_status", {}],
      ["bgi_native_catalog", {}], ["bgi_native_call", { name: "get_bgi_version" }],
      ["bgi_native_http", { kind: "health" }], ["bgi_native_logs", { duration_ms: 100, limit: 1 }],
    ]) {
      const result = await send("tools/call", { name, arguments: args });
      if (result.isError || !result.content?.length) throw new Error(name + " 自检失败 " + JSON.stringify(result));
      if (name === "gctl_status" && !JSON.parse(result.content[0].text).bettergi_installed) throw new Error("引擎没有就位");
    }
    return { version: init.serverInfo.version, tools: tools.length, python: py.trim(), powershell: psh.trim() };
  } finally {
    child.stdin.end();
    reader.close();
  }
}
