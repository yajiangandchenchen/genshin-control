# 第三方声明

完整集成发行采用 GNU GPL 第 3 版。`server/`、`scripts/`、`setup.cmd`、测试和项目文档中属于本项目的原创部分，也可选择 MIT 许可。第三方文件保留原作者、原许可证和随附声明。 [GPLv3 正文](LICENSE) · [自有部分 MIT 正文](licenses/MIT.txt)

## BetterGI 和发布来源

- **BetterGI 0.67.0** 是游戏视觉和自动化底层。`bundles/bettergi-0.67.0/` 是官方便携 7z 按字节切分后的内容，拼合后的 SHA-256 为 `7bbe9f9a108b46cb9ae2e9164d3fbbfe7f8655a9ab9c377a203064a45a822079`。本项目通过普通进程、公开脚本接口以及引擎的调度器与其通信。 [上游项目](https://github.com/babalae/better-genshin-impact) · [官方发行](https://github.com/babalae/better-genshin-impact/releases/tag/0.67.0)
- 本仓库附有该次发布工作流实际检出的程序源码 `841443d6f15fcb60062b24cb8153fae106186daf`。工作流在编译前写入了版本号 `0.67.0`，随后按其发布配置构建 Windows x64 便携程序。源码归档含构建脚本和依赖版本信息。 [随包构建源码](third_party/source/bettergi-0.67.0-build-source.zip) · [实际构建记录](https://github.com/babalae/better-genshin-impact/actions/runs/42665805831) · [固定提交](https://github.com/babalae/better-genshin-impact/tree/841443d6f15fcb60062b24cb8153fae106186daf)
- **模型与数据**。BetterGI 随包的 `Assets/Model/LICENSE` 含模型许可说明，明确要求相关集成代码公开并按 GPLv3 提供。该声明随本仓库和官方归档一同提供。Silero VAD 保留 MIT，词典目录保留 Apache-2.0。 [模型说明全文](licenses/bettergi/MODELS.txt) · [Silero 声明](licenses/bettergi/SILERO.txt) · [词典声明](licenses/bettergi/DICTIONARIES.txt)
- **发布资源和网页界面**。数据包取自 BetterGI 发布流程使用的 `bettergi-publish`。地图编辑器与脚本库网页保留官方发行包的产物。脚本库网页的对应 GPLv3 源码也随仓库提供。地图编辑器请查阅其原项目文件和权利说明。 [构建使用的提交清单](third_party/source/release-checkouts.json) · [发布资源](https://github.com/babalae/bettergi-publish/tree/d7538a2b2a294a760c11d7c1fb9d3325dec2a5ef) · [地图编辑器精确源码](https://github.com/huiyadanli/bettergi-map/tree/ea2935fc1c6d28e83c68bfbd81dd7839bb3b6a93) · [网页源码归档](third_party/source/bettergi-script-web-697b67c.zip) · [网页许可](licenses/bettergi/SCRIPT-WEB-GPL.txt)
- BetterGI 便携包原有的 .NET、原生库、WebView2 加载器和其他资源以原始归档交付，其原有标识和许可继续适用。网页编辑器使用 Windows 的 Microsoft Edge WebView2 运行时，与上游部署方式一致。 [Microsoft WebView2 分发说明](https://learn.microsoft.com/microsoft-edge/webview2/concepts/distribution)

## Agent 桥接

| 原项目 | 本仓库保留的内容与修改 | 协议 |
| --- | --- | --- |
| [gychen-NJU/bettergi-mcp](https://github.com/gychen-NJU/bettergi-mcp/tree/670d01f198150c1e625f69aff88718b05fb05344) | 29 个工具、运行条件判断、PowerShell 参数启动器。适配了随包运行时、带空格或中文的路径和本地缓存。 | [MIT](vendor/bettergi-mcp/LICENSE) |
| [gychen-NJU/bettergi-control](https://github.com/gychen-NJU/bettergi-control/tree/3111d0f108ab782042b20c4d2598b8f329796c77) | 15 个运行脚本，覆盖直接工具调用和公共路径解析。安装定位、进程处理和诊断适配了本项目目录。 | [MIT](vendor/bettergi-control/LICENSE) |

由用户选择下载的社区路线、战斗策略及第三方 JavaScript 各自适用作者声明。资源检索和脚本安装可访问 BetterGI 官方社区仓库。 [社区仓库](https://github.com/babalae/bettergi-scripts-list)

## 独立运行时

| 组件 | 随包内容 | 许可与来源 |
| --- | --- | --- |
| Node.js 22.19.0 x64 | 官方 `node.exe`，用于启动 stdio 服务 | [Node.js 及内含组件的完整许可](licenses/node/LICENSE) · [官方校验表](https://nodejs.org/dist/v22.19.0/SHASUMS256.txt) |
| PowerShell 7.6.9 x64 | 官方 Windows ZIP 便携运行时 | [MIT](licenses/powershell/LICENSE.txt) · [第三方声明](licenses/powershell/ThirdPartyNotices.txt) · [上游发行](https://github.com/PowerShell/PowerShell/releases/tag/v7.6.9) |
| Python 3.13.17 x64 | Python Software Foundation 的嵌入式 Windows 发行，加上项目模块搜索路径 | [PSF 及随附许可](licenses/python/CPython.txt) · [上游发行](https://www.python.org/downloads/release/python-31317/) |
| Python wheel | comtypes 1.5.0、DXcam 0.3.0、MSS 10.2.0、NumPy 2.5.5、opencv-python-headless 5.1.0.95、PyDirectInput 1.0.4。各 wheel 内的原始版权和第三方库声明保留在安装包中。 | [逐包许可证](licenses/python/) · [PyPI 下载地址及校验](scripts/wheels.lock.json) |
| 7-Zip Extra 26.06 x64 | Igor Pavlov 的 `7za.exe`，用于离线解包 | [原许可及 LGPL/BSD 条款](licenses/7zip/LICENSE.txt) · [对应源码](third_party/source/7zip-26.06-src.7z) · [官方发行](https://github.com/ip7z/7zip/releases/tag/26.06) |

每个组件的归档摘要、分卷顺序、字节数和逐卷 SHA-256 均记录在清单里。维护者可以用核对过的官方归档及锁定的原始 wheel 重建分卷。 [离线清单](bundles/manifest.json) · [重打包脚本](scripts/package_runtime.py)
