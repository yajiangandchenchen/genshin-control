# Genshin Control

Genshin Control 是运行在 Windows x64 上的本地 stdio MCP 服务。Codex、Claude Code 和其他支持 stdio 的 Agent 可以通过同一个入口读取原神画面状态、控制键鼠、运行本地视觉挑战或调度内置的 BetterGI。

## 安装

1. 下载仓库的完整 ZIP，全部解压到本机一个可写目录。离线分卷、运行程序和源码一起交付。 [下载 main 分支 ZIP](https://github.com/yajiangandchenchen/genshin-control/archive/refs/heads/main.zip)
2. 在包含 `setup.cmd` 的目录打开 PowerShell，运行以下命令。安装器逐卷核对 SHA-256，解包到 `.runtime`，并部署 BetterGI 场景脚本。自检会启动 MCP、检查工具发现并执行只读调用。

   ```powershell
   .\setup.cmd --verify
   ```

3. 在下面选择你的客户端完成注册。在客户端的 MCP 列表里看到 `genshin_control` 后，可以先请 Agent 调用 `gctl_status` 检查当前状态。

发行包内含 BetterGI 0.67.0、Node.js 22.19.0、CPython 3.13.17、PowerShell 7.6.9、7-Zip 解包器和锁定的 Python 库。首次安装从本地分卷完成。实际游戏操作需要已安装的《原神》、当前用户的交互式 Windows 桌面和一个 MCP 客户端。BetterGI 的可选网页编辑器沿用 Windows 的 WebView2。在线订阅、查询新的社区路线和下载第三方脚本时会访问对应上游。

## 接入 Codex

在解压后的项目根目录运行。Codex 桌面端使用当前用户的 MCP 配置。

```powershell
$mcpNode = (Resolve-Path .\bin\node.exe).Path
$mcpEntry = (Resolve-Path .\server\mcp.mjs).Path
codex mcp add genshin_control -- $mcpNode $mcpEntry
codex mcp get genshin_control
```

如果已有对话还显示旧工具清单，在客户端重新连接该服务或新开一个会话。 [Codex MCP 文档](https://developers.openai.com/codex/mcp/)

## 接入 Claude Code

在同一个目录运行。`--scope user` 供当前用户的项目使用，`--scope project` 将注册写入当前项目的 `.mcp.json`。

```powershell
$mcpNode = (Resolve-Path .\bin\node.exe).Path
$mcpEntry = (Resolve-Path .\server\mcp.mjs).Path
claude mcp add --transport stdio --scope user genshin_control -- $mcpNode $mcpEntry
claude mcp get genshin_control
```

在 Claude Code 会话中也可以输入 `/mcp` 查看连接状态。 [Claude Code MCP 文档](https://code.claude.com/docs/en/mcp)

## 接入其他 MCP 客户端

执行 `setup.cmd --config` 会输出带有本机实际绝对路径的 `mcpServers` JSON。支持该格式的客户端可直接使用。对于提供表单或采用其他 JSON 键名的客户端，选择 `stdio`，将生成的 `command` 填入程序栏，将 `args` 中的每一项单独填入参数栏。

```powershell
.\setup.cmd --config
```

客户端直接启动仓库里的 `bin/node.exe`，参数为 `server/mcp.mjs` 的绝对路径。移动项目目录后，在新目录重新执行注册命令即可。

## 游戏路径与使用

启动游戏后，控制器会识别国服 `YuanShen.exe` 或国际服 `GenshinImpact.exe`。有多个实例或希望指定安装位置时，可以保存本机路径。

```powershell
$gamePath = Read-Host '输入游戏可执行文件的完整路径'
.\setup.cmd --game $gamePath
```

运行时也可用 `GENSHIN_CONTROL_GAME_EXE` 为当前客户端覆盖这个值。实时输入会核对前台游戏窗口，连续动作结束、失焦或急停时释放持有的按键。执行中的本地输入可以用 `Ctrl+Alt+F10` 或 `gctl_stop` 急停，用 `gctl_resume` 恢复。

| 用途 | 工具 |
| --- | --- |
| 看画面与状态 | `gctl_frame` 返回游戏客户区或指定区域的 JPEG。`gctl_observe` 返回场景、移动姿态、目标和采样耗时。 |
| 连续运动与视觉挑战 | `gctl_act` 组合长按和转镜。`gctl_job_start`、`gctl_job_status`、`gctl_job_cancel` 调度本地闭环阶段，支持自定义模板、目标丢失检测和阶段成功或失败提示。 |
| 调用 BetterGI | `bgi_scene_start` 支持观测、战斗策略、自动战斗、指定路线、七种移动方式的途经点、传送和对话。`bgi_scene_status` 读回执，`bgi_scene_release` 释放引擎控制。 |
| 脚本、策略与调度 | 保留上游 29 个 `bgi_*` 工具，包括配置组、脚本管理、路线检索、运行监控、日志和诊断。 |
| 引擎健康、版本和日志 | `bgi_native_*` 优先使用引擎提供的本机服务。随包的 0.67.0 使用标明来源的进程、可执行文件和本地日志兼容查询。 |

47 个工具的参数以当前服务返回的 `tools/list` 为准。场景方法返回和实际游戏任务完成各有独立的回执或画面证据。具体挑战需要适合该场景的目标、路线或策略。

## 目录和本机数据

| 目录 | 内容 |
| --- | --- |
| `server/` | 通用 MCP 服务、实时视觉与输入、BetterGI 场景脚本、测试 |
| `vendor/` | MIT 上游桥接的运行文件和 PowerShell 执行脚本 |
| `bin/`、`bundles/` | 直接启动的程序、离线分卷、版本和完整性清单 |
| `scripts/` | 离线安装、自检和维护者重新打包工具 |
| `licenses/`、`third_party/source/` | 第三方版权声明、对应引擎和部分上游源码 |
| `.local/` | 这台机器的游戏路径、模板和缓存 |
| `.runtime/` | 解包后的运行时；BetterGI 的 `User/` 和 `log/` 位于对应引擎目录内 |

备份时保存 `.local/` 和引擎的 `BetterGI/User/`。重复运行安装会复用已校验的组件，并同步本项目的场景脚本。首次使用另一版引擎时，会从上次记录的引擎目录迁移用户配置。客户端自身的注册文件由各客户端管理。

## 许可与来源

完整集成以 **GPLv3** 公开发布。本项目自行创作的 MCP、控制器、安装脚本、测试及文档同时提供 **MIT** 许可。BetterGI 自训模型的全链路公开说明、上游桥接及其余独立运行时各自的条款列在第三方清单中。 [集成许可证](LICENSE) · [项目自有代码的 MIT 许可](licenses/MIT.txt) · [上游声明与源码](THIRD_PARTY.md)
