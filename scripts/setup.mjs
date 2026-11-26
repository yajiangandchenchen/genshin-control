// SPDX-License-Identifier: GPL-3.0-only OR MIT
import { paths, prepare, setGame, verifyBundles } from "./runtime.mjs";
import { verifyServer } from "./verify.mjs";

const args = process.argv.slice(2);
try {
  const options = new Set();
  let game;
  for (let n = 0; n < args.length; n++) {
    const name = args[n];
    if (name === "--game") {
      game = args[++n];
      if (!game) throw new Error("--game 需要游戏可执行文件路径");
    } else if (["--config", "--verify", "--help"].includes(name)) {
      options.add(name);
    } else {
      throw new Error("未知参数 " + name);
    }
  }
  if (options.has("--help")) {
    console.log("setup.cmd [--game 游戏可执行文件] [--verify] [--config]");
  } else {
    if (game) await setGame(game);
    await prepare((message) => console.error(message));
    if (options.has("--verify")) {
      await verifyBundles();
      const result = await verifyServer();
      console.error("离线自检通过 " + JSON.stringify(result));
    }
    const configuration = { mcpServers: { genshin_control: { command: paths.node, args: [paths.entry] } } };
    if (options.has("--config")) console.log(JSON.stringify(configuration, null, 2));
    else console.log("运行环境已就绪。执行 setup.cmd --config 可生成当前目录的 MCP 配置。");
  }
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
