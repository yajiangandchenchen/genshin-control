// SPDX-License-Identifier: GPL-3.0-only OR MIT
import { execFile } from "node:child_process";
import { open, readdir } from "node:fs/promises";
import path from "node:path";
import { setTimeout as wait } from "node:timers/promises";
import { promisify } from "node:util";
import { paths } from "../scripts/runtime.mjs";

const exec = promisify(execFile);
const readEngine = [
  "$ErrorActionPreference='Stop'",
  "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)",
  "$file=Get-Item -LiteralPath $env:GENSHIN_CONTROL_DIAGNOSTIC_ENGINE",
  "$p=Get-Process -Name BetterGI -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $file.FullName } | Select-Object -First 1",
  "[pscustomobject]@{version=$file.VersionInfo.ProductVersion;running=[bool]$p;processId=if($p){$p.Id}else{$null};uptimeSeconds=if($p){[int]((Get-Date)-$p.StartTime).TotalSeconds}else{$null}} | ConvertTo-Json -Compress",
].join("\n");

export async function engineDiagnostic(kind) {
  const { stdout } = await exec(paths.powershell, ["-NoProfile", "-NonInteractive", "-Command", readEngine], {
    windowsHide: true, timeout: 8000, encoding: "utf8",
    env: { ...process.env, GENSHIN_CONTROL_DIAGNOSTIC_ENGINE: path.join(paths.engine, "BetterGI.exe") },
  });
  const data = JSON.parse(stdout.trim());
  return kind === "version" ? { source: "executable-metadata", version: data.version, nativeApiVersion: null }
                            : { source: "windows-process", ...data };
}

const diagnostics = { get_bgi_health: ["health", "读取内置 BetterGI 的进程状态"],
                      get_bgi_version: ["version", "读取内置 BetterGI 可执行文件版本"] };
export async function compatibilityMcp(method, params) {
  if (method === "tools/list") {
    return { source: "portable-diagnostics", tools: Object.entries(diagnostics).map(([name, [, description]]) => ({
      name, description, inputSchema: { type: "object", properties: {}, additionalProperties: false },
      annotations: { readOnlyHint: true },
    })) };
  }
  if (method !== "tools/call" || !Object.hasOwn(diagnostics, params.name)) throw new Error("当前引擎未提供该原生工具");
  const value = await engineDiagnostic(diagnostics[params.name][0]);
  return { content: [{ type: "text", text: JSON.stringify(value) }], structuredContent: value };
}

const levels = { Verbose: 0, Debug: 1, Information: 2, Warning: 3, Error: 4, Fatal: 5 };
const names = { VRB: "Verbose", DBG: "Debug", INF: "Information", WRN: "Warning", ERR: "Error", FTL: "Fatal" };
async function logTail(directory) {
  let files;
  try { files = await readdir(directory); }
  catch (error) { if (error.code === "ENOENT") return null; throw error; }
  const name = files.filter((entry) => /^better-genshin-impact\d+\.log$/i.test(entry)).sort().at(-1);
  if (!name) return null;
  const file = await open(path.join(directory, name), "r");
  try {
    const { size } = await file.stat();
    const count = Math.min(size, 192 * 1024);
    const buffer = Buffer.alloc(count);
    const { bytesRead } = await file.read(buffer, 0, count, size - count);
    const entries = [];
    for (const line of buffer.toString("utf8", 0, bytesRead).split(/\r?\n/)) {
      const match = line.match(/^\[([^\]]+)\]\s+\[(VRB|DBG|INF|WRN|ERR|FTL)\]\s*(.*)/);
      if (match) entries.push({ timestamp: match[1], level: names[match[2]], message: match[3] });
      else if (line && entries.length) entries.at(-1).message += "\n" + line;
    }
    return { name, size, entries };
  } finally { await file.close(); }
}

export async function compatibilityLogs(duration, limit, minimum) {
  const end = Date.now() + duration;
  const directory = path.join(paths.engine, "log");
  let snapshot = await logTail(directory);
  const firstSize = snapshot?.size;
  if (!snapshot || snapshot.entries.filter(e => levels[e.level] >= levels[minimum]).length < limit) {
    while (Date.now() < end) {
      await wait(Math.min(180, end - Date.now()));
      snapshot = await logTail(directory);
      if (snapshot && snapshot.size !== firstSize) break;
    }
  }
  const records = (snapshot?.entries ?? []).filter(e => levels[e.level] >= levels[minimum]).slice(-limit);
  return { source: "bettergi-log-file", opened: Boolean(snapshot), records };
}
