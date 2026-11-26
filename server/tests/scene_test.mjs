// SPDX-License-Identifier: GPL-3.0-only OR MIT
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../bettergi-extension/main.js", import.meta.url), "utf8");

async function simulate(operation, args) {
  const files = new Map([["request.json", JSON.stringify({ id: "offline", operation, arguments: args })]]);
  const routed = [];
  let disposed = 0;
  const scope = {
    file: {
      IsFile: (name) => files.has(name),
      ReadTextSync: (name) => files.get(name),
      WriteTextSync: (name, data) => { files.set(name, data); return true; },
    },
    genshin: {
      Width: 2560, Height: 1440, ScaleTo1080PRatio: 4 / 3,
      GetCameraOrientation: () => 185,
      GetPositionFromMap: (map, ttl) => ({ X: 12, Y: -34, map, ttl }),
    },
    captureGameRegion: () => ({
      Width: 2560, Height: 1440,
      FindMulti: () => ({ Count: 1, 0: { Text: "寻找并收集符文", Dispose() { disposed++; } } }),
      Dispose() { disposed++; },
    }),
    RecognitionObject: { Ocr: (...coordinates) => coordinates },
    getAvatars: () => ({ Length: 2, 0: "旅行者", 1: "奥黛塔" }),
    sleep: async () => {},
    log: { info: () => {} },
    pathingScript: { Run: async (data) => { routed.push(JSON.parse(data)); return true; } },
  };
  await vm.runInNewContext(source, scope);
  return { result: JSON.parse(files.get("result.json")), files, routed, disposed };
}

const observed = await simulate("observe", { map: "Teyvat", position: true, samples: 3,
                                             interval_ms: 200, cache_ms: 300, party: true, quest: true });
assert.equal(observed.result.state, "completed");
assert.equal(observed.result.result.latest.position.x, 12);
assert.equal(observed.result.result.latest.quest_ocr[0], "寻找并收集符文");
assert.equal(observed.result.result.latest.party.length, 2);
assert.equal(observed.disposed, 2);
assert.equal(JSON.parse(observed.files.get("telemetry.json")).sample, 3);
const routed = await simulate("navigate", { map: "Teyvat", points: [
  { x: 1, y: 2, mode: "walk", type: "path" }, { x: 3, y: 4, mode: "fly", type: "target" },
] });
assert.equal(routed.result.result.completed, true);
assert.equal(routed.routed[0].info.map_name, "Teyvat");
assert.equal(routed.routed[0].positions[1].move_mode, "fly");
process.stdout.write("BetterGI 宿主脚本离线协议验证通过\n");
