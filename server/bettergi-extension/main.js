// SPDX-License-Identifier: GPL-3.0-only OR MIT
(async function () {
  const request = JSON.parse(file.ReadTextSync("request.json"));
  if (typeof request.id !== "string" || !request.id) throw new Error("缺少本机场景任务编号");
  if (file.IsFile("result.json")) {
    const prior = JSON.parse(file.ReadTextSync("result.json"));
    if (prior.id === request.id) return;
  }

  function report(state, result) {
    const data = { id: request.id, operation: request.operation, state, result };
    if (!file.WriteTextSync("result.json", JSON.stringify(data))) {
      throw new Error("无法写入场景任务状态");
    }
  }

  function guarded(label, errors, action) {
    try {
      return action();
    } catch (error) {
      errors[label] = String(error.message || error);
      return null;
    }
  }

  function readQuest() {
    const frame = captureGameRegion();
    try {
      const left = Math.round(frame.Width * .018);
      const top = Math.round(frame.Height * .152);
      const cue = RecognitionObject.Ocr(left, top, Math.round(frame.Width * .28), Math.round(frame.Height * .22));
      const rows = frame.FindMulti(cue);
      const text = [];
      for (let index = 0; index < rows.Count; index++) {
        const row = rows[index];
        try {
          if (row.Text) text.push(String(row.Text).trim());
        } finally {
          row.Dispose();
        }
      }
      return text;
    } finally {
      frame.Dispose();
    }
  }

  async function readState(args) {
    const errors = {};
    const started = Date.now();
    const point = args.position ? guarded("position", errors, () => {
      const value = genshin.GetPositionFromMap(args.map, args.cache_ms);
      if (!value) return null;
      const x = Number(value.X);
      const y = Number(value.Y);
      return Number.isFinite(x) && Number.isFinite(y) ? { x, y, map: args.map } : null;
    }) : null;
    const heading = guarded("camera", errors, () => {
      const value = Number(genshin.GetCameraOrientation());
      return Number.isFinite(value) ? value : null;
    });
    const quest = args.quest ? guarded("quest_ocr", errors, readQuest) : null;
    const party = args.party ? guarded("party", errors, () => {
      const names = getAvatars();
      const result = [];
      for (let index = 0; index < names.Length; index++) result.push(String(names[index]));
      return result;
    }) : null;
    return { timestamp_ms: Date.now(), width: Number(genshin.Width), height: Number(genshin.Height),
             position: point, camera: heading, quest_ocr: quest, party, errors, sample_ms: Date.now() - started };
  }

  // 只调用 BetterGI 的公开脚本方法，任务数据从本地 JSON 传入。
  const actions = {
    probe: () => ({
      width: Number(genshin.Width),
      height: Number(genshin.Height),
      scale: Number(genshin.ScaleTo1080PRatio),
      operations: {
        combat_script: typeof dispatcher.RunCombatScript !== "undefined",
        auto_fight: typeof dispatcher.RunAutoFightTask !== "undefined",
        pathing: typeof pathingScript.RunFileFromUser !== "undefined",
        teleport: typeof genshin.Tp !== "undefined",
        dialogue: typeof genshin.ChooseTalkOption !== "undefined",
      },
    }),
    observe: async (args) => {
      const samples = [];
      for (let index = 0; index < args.samples; index++) {
        if (index > 0) await sleep(args.interval_ms);
        const state = await readState({ ...args, quest: args.quest && index === args.samples - 1,
                                        party: args.party && index === args.samples - 1 });
        samples.push(state);
        const packet = { id: request.id, sample: index + 1, total: args.samples, state };
        if (!file.WriteTextSync("telemetry.json", JSON.stringify(packet))) {
          throw new Error("无法写入 BetterGI 状态反馈");
        }
      }
      return { latest: samples[samples.length - 1], samples };
    },
    combat_script: async (args) => {
      await dispatcher.RunCombatScript(args.script, args.avatar || null);
      return { executed: true };
    },
    auto_fight: async (args) => {
      const options = new AutoFightParam(args.strategy);
      options.Timeout = args.timeout_seconds;
      options.PickDropsAfterFightEnabled = Boolean(args.loot);
      options.FightFinishDetectEnabled = true;
      await dispatcher.RunAutoFightTask(options);
      return { task_returned: true };
    },
    pathing: async (args) => ({ completed: await pathingScript.RunFileFromUser(args.route) }),
    navigate: async (args) => {
      const positions = args.points.map((point) => ({
        x: point.x, y: point.y, type: point.type, move_mode: point.mode,
      }));
      const route = { info: { name: "Codex 场景寻路", map_name: args.map }, positions };
      const completed = await pathingScript.Run(JSON.stringify(route));
      return { completed: Boolean(completed), point_count: positions.length, map: args.map };
    },
    teleport: async (args) => {
      if (args.map) await genshin.Tp(args.x, args.y, args.map, false);
      else await genshin.Tp(args.x, args.y);
      return { task_returned: true, x: args.x, y: args.y };
    },
    dialogue: async (args) => {
      await genshin.ChooseTalkOption(args.option, args.skip_times, Boolean(args.orange));
      return { task_returned: true };
    },
  };
  if (!Object.prototype.hasOwnProperty.call(actions, request.operation)) {
    throw new Error("场景任务类型不支持");
  }
  report("running", null);
  try {
    const result = await actions[request.operation](request.arguments);
    report("completed", result);
    log.info("Codex 场景任务 " + request.id + " 已返回");
  } catch (error) {
    report("error", String(error.message || error));
    throw error;
  }
})();
