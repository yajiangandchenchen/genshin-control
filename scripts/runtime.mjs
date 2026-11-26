// SPDX-License-Identifier: GPL-3.0-only OR MIT
import { createHash, randomUUID } from "node:crypto";
import { createReadStream, createWriteStream, existsSync, lstatSync, readFileSync } from "node:fs";
import { cp, mkdir, mkdtemp, readFile, rename, rm, stat, writeFile } from "node:fs/promises";
import { execFile } from "node:child_process";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import { promisify } from "node:util";
import path from "node:path";
import { fileURLToPath } from "node:url";

const exec = promisify(execFile);
export const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
export const localRoot = path.join(root, ".local");
const bundles = path.join(root, "bundles");
const installed = path.join(root, ".runtime");
export const manifest = JSON.parse(readFileSync(path.join(bundles, "manifest.json"), "utf8"));
if (manifest.format !== 1 || manifest.platform !== "win32-x64") throw new Error("离线清单格式无效");

function folderName(key) {
  const spec = manifest.components[key];
  if (!/^[a-z0-9_-]+$/.test(key) || !/^[0-9.]+$/.test(spec.version) || !/^[a-f0-9]{64}$/.test(spec.sha256)) {
    throw new Error("离线清单中的组件名称或版本无效");
  }
  return key + "-" + spec.version + "-" + spec.sha256.slice(0, 12);
}
const folder = (key) => path.join(installed, folderName(key));
export const paths = {
  node: path.join(root, "bin", "node.exe"),
  archiver: path.join(root, "bin", "7za.exe"),
  entry: path.join(root, "server", "mcp.mjs"),
  engine: path.join(folder("bettergi"), "BetterGI"),
  python: path.join(folder("python"), "python.exe"),
  powershell: path.join(folder("powershell"), "pwsh.exe"),
  legacy: path.join(root, "vendor", "bettergi-mcp", "bettergi-mcp.mjs"),
  controls: path.join(root, "vendor", "bettergi-control", "scripts"),
  state: path.join(localRoot, "state"),
  settings: path.join(localRoot, "settings.json"),
};
export function settings() {
  const value = existsSync(paths.settings) ? JSON.parse(readFileSync(paths.settings, "utf8").replace(/^\uFEFF/, "")) : {};
  const game = process.env.GENSHIN_CONTROL_GAME_EXE || value.game_exe;
  if (game && (!path.isAbsolute(game) || !/^(yuanshen|genshinimpact)\.exe$/i.test(path.basename(game)))) {
    throw new Error("游戏路径应指向 YuanShen.exe 或 GenshinImpact.exe");
  }
  return { game_exe: game || null };
}

export async function hashFile(file) {
  const hash = createHash("sha256");
  for await (const chunk of createReadStream(file)) hash.update(chunk);
  return hash.digest("hex");
}
async function atomicWrite(file, data) {
  const temp = file + "." + randomUUID() + ".partial";
  try {
    await writeFile(temp, data, { flag: "wx" });
    await rename(temp, file);
  } finally {
    await rm(temp, { force: true });
  }
}
export async function setGame(file) {
  if (!path.isAbsolute(file) || !/^(yuanshen|genshinimpact)\.exe$/i.test(path.basename(file)) || !(await stat(file)).isFile()) {
    throw new Error("请传入实际游戏可执行文件的完整路径");
  }
  await mkdir(localRoot, { recursive: true });
  const value = existsSync(paths.settings) ? JSON.parse(await readFile(paths.settings, "utf8")) : {};
  await atomicWrite(paths.settings, JSON.stringify({ ...value, game_exe: path.normalize(file) }, null, 2) + "\n");
}

function member(base, rel) {
  const target = path.resolve(base, rel);
  if (!target.startsWith(base + path.sep) || path.isAbsolute(rel) || rel.includes(":")) throw new Error("离线包路径越界");
  return target;
}
async function complete(target, spec) {
  try {
    const receipt = JSON.parse(await readFile(path.join(target, ".bundle.json"), "utf8"));
    if (receipt.sha256 !== spec.sha256) return false;
    for (const name of spec.required) {
      const file = await stat(member(target, name));
      if (!file.isFile() || !file.size) return false;
    }
    return true;
  } catch (error) {
    if (error.code === "ENOENT" || error instanceof SyntaxError) return false;
    throw error;
  }
}

async function* verifiedParts(spec) {
  const total = createHash("sha256");
  let length = 0;
  for (const part of spec.parts) {
    const digest = createHash("sha256");
    let partSize = 0;
    for await (const block of createReadStream(member(bundles, part.file))) {
      partSize += block.length;
      digest.update(block);
      total.update(block);
      yield block;
    }
    if (partSize !== part.bytes || digest.digest("hex") !== part.sha256) throw new Error("分卷完整性校验失败：" + part.file);
    length += partSize;
  }
  if (length !== spec.bytes || total.digest("hex") !== spec.sha256) throw new Error("原始归档完整性校验失败：" + spec.archive);
}
export async function verifyBundles() {
  for (const [name, checksum] of Object.entries(manifest.bootstrap)) {
    if (await hashFile(member(path.join(root, "bin"), name)) !== checksum) throw new Error("启动组件校验失败：" + name);
  }
  for (const spec of Object.values(manifest.components)) {
    for await (const _ of verifiedParts(spec)) { /* 读取所有卷完成校验。 */ }
  }
}

async function previousEngineUser() {
  try {
    const pointer = JSON.parse(await readFile(path.join(localRoot, "engine.json"), "utf8"));
    if (!/^bettergi-[0-9.]+-[a-f0-9]{12}$/.test(pointer.directory)) return null;
    const previous = path.join(installed, pointer.directory, "BetterGI", "User");
    return existsSync(previous) ? previous : null;
  } catch (error) {
    if (error.code === "ENOENT") return null;
    throw error;
  }
}
async function installComponent(key, report) {
  const spec = manifest.components[key];
  const destination = folder(key);
  if (await complete(destination, spec)) return;
  report("正在从仓库解包 " + key + " " + spec.version);
  const work = await mkdtemp(path.join(installed, ".extract-" + key + "-"));
  try {
    const archive = member(work, spec.archive);
    const staging = path.join(work, "contents");
    await pipeline(Readable.from(verifiedParts(spec)), createWriteStream(archive, { flags: "wx" }));
    await mkdir(staging);
    await exec(paths.archiver, ["x", "-y", "-bd", "-bso0", "-bsp0", "-bse2", "-o" + staging, archive], {
      windowsHide: true, maxBuffer: 1024 * 1024,
    });
    for (const name of spec.required) {
      if (!(await stat(member(staging, name))).isFile()) throw new Error("组件缺少 " + name);
    }
    if (key === "bettergi") {
      const oldUser = await previousEngineUser();
      if (oldUser) {
        await cp(oldUser, path.join(staging, "BetterGI", "User"), {
          recursive: true, filter: (src) => !lstatSync(src).isSymbolicLink(),
        });
      }
    }
    await writeFile(path.join(staging, ".bundle.json"), JSON.stringify({ sha256: spec.sha256, version: spec.version }));
    try {
      await rename(staging, destination);
    } catch (error) {
      if (await complete(destination, spec)) return;
      if (!existsSync(destination)) throw error;
      // 修复不完整的旧目录时保留其原件。并发安装完成的目录无需替换。
      const backup = destination + ".backup-" + randomUUID();
      await rename(destination, backup);
      try {
        await rename(staging, destination);
      } catch (publishError) {
        if (await complete(destination, spec)) return;
        if (!existsSync(destination)) await rename(backup, destination);
        throw publishError;
      }
    }
  } finally {
    await rm(work, { recursive: true, force: true, maxRetries: 3, retryDelay: 160 });
  }
}

async function syncExtension() {
  const scene = path.join(paths.engine, "User", "JsScript", "CodexScene");
  await mkdir(scene, { recursive: true });
  for (const name of ["main.js", "manifest.json"]) {
    const source = await readFile(path.join(root, "server", "bettergi-extension", name));
    const target = path.join(scene, name);
    if (!existsSync(target) || !source.equals(await readFile(target))) await atomicWrite(target, source);
  }
  const config = path.join(paths.engine, "User", "config.json");
  try {
    await writeFile(config, JSON.stringify({ autoPickConfig: { enabled: false }, autoSkipConfig: { enabled: false } }), { flag: "wx" });
  } catch (error) {
    if (error.code !== "EEXIST") throw error;
  }
  const marker = path.join(localRoot, "engine.json");
  const note = JSON.stringify({ directory: folderName("bettergi") });
  if (!existsSync(marker) || (await readFile(marker, "utf8")) !== note) await atomicWrite(marker, note);
}

let preparing;
export function prepare(report = () => {}) {
  if (preparing) return preparing;
  preparing = (async () => {
    if (process.platform !== "win32" || process.arch !== "x64") throw new Error("此发行包面向 Windows x64");
    await Promise.all([mkdir(installed, { recursive: true }), mkdir(paths.state, { recursive: true })]);
    if (await hashFile(paths.archiver) !== manifest.bootstrap["7za.exe"]) throw new Error("内置解包器校验失败");
    for (const key of Object.keys(manifest.components)) await installComponent(key, report);
    await syncExtension();
    return paths;
  })().catch((error) => { preparing = undefined; throw error; });
  return preparing;
}
