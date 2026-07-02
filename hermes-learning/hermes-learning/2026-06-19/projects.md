# 2026-06-19 全网优秀项目学习记录

> 来源：GitHub 新近高星仓库检索、项目 README/目录结构、arXiv 最新论文检索。昨日归档 `/opt/hermes-learning/2026-06-18/archive.json` 已检查，本日未重复记录既有项目。

## 1. XiaomiMiMo/MiMo-Code

- GitHub：https://github.com/XiaomiMiMo/MiMo-Code
- 一句话定位：终端原生 AI 编程助手，强调“模型与 Agent 共同进化”、持久记忆与长程任务连续执行。
- 领域标签：AI/LLM、Agent、开发者工具、CLI、代码协作

**核心架构/技术亮点**

1. 多 Agent 模式明确区分 `build`、`plan`、`compose`：`build` 拥有完整开发权限，`plan` 只读分析，`compose` 面向规格驱动与技能编排。这种权限与职责分层比单一“聊天式助手”更适合长任务。
2. 持久记忆系统以 SQLite FTS5 为底座，拆成项目记忆 `MEMORY.md`、会话检查点 `checkpoint.md`、临时笔记 `notes.md`、任务进度 `tasks/<id>/progress.md`。它把“长期项目知识”和“短期会话状态”分层保存，便于恢复上下文。
3. 上下文管理不是简单截断，而是根据上下文窗口自动生成检查点，在接近限制时从检查点、项目记忆、任务进度和最近消息重建上下文，并通过 token budget 控制注入量。
4. 任务系统采用树形编号如 `T1`、`T1.1`、`T1.2`，天然适配分解任务、子任务进展与恢复执行，和检查点系统形成闭环。
5. `/goal` 命令引入独立 judge 模型判断停止条件是否真正满足，专门防止自主 Agent 在长程执行中“乐观提前结束”。

**代码片段或设计模式**

```json
{
  "scripts": {
    "dev": "MIMOCODE_HOME=$PWD/.dev-home bun run --cwd packages/opencode --conditions=browser src/index.ts",
    "lint": "oxlint",
    "typecheck": "bun turbo typecheck",
    "test": "echo 'do not run tests from root' && exit 1"
  },
  "workspaces": ["packages/*", "packages/console/*", "packages/sdk/js"]
}
```

这个 monorepo 配置体现出“根目录不直接跑测试、按包拆分工作区”的工程策略：避免误跑大范围测试，同时把 CLI、桌面、Web、SDK 等交付形态放在统一依赖目录中演进。

**可借鉴点**

Hermes 可重点借鉴它的“分层记忆 + 停止条件审计”。现在很多 Agent 的失败不是工具不够，而是任务执行到一半丢失意图或过早停下。Hermes 若把 cron、长期任务、项目记忆、用户偏好和最近执行状态拆成可追踪文件，并在每次准备结束前由轻量 verifier 检查“用户目标是否满足”，能显著提升自动任务可信度。另外，`plan/build/compose` 模式也可映射为 Hermes 的只读审阅、执行修复、流程编排三类运行配置，降低误操作风险。

---

## 2. AprilNEA/OpenLogi

- GitHub：https://github.com/AprilNEA/OpenLogi
- 一句话定位：Rust 编写的本地优先 Logitech Options+ 替代品，支持鼠标按键重映射、DPI 与 SmartShift，无账号、无遥测。
- 领域标签：优秀 App、桌面应用、Rust、HID、Linux/macOS、本地优先

**核心架构/技术亮点**

1. 双二进制架构：`openlogi-gui` 提供 GPUI 桌面界面，`openlogi-cli` 提供无头设备枚举、资源同步、HID++ 诊断等能力。GUI 与 CLI 分工清晰，便于高级用户脚本化，也利于自动化测试。
2. 本地优先：配置以 plain TOML 存储，按钮映射、DPI、SmartShift 都直接走本地 OS event tap、evdev/uinput 或 HID++ 写入设备，不依赖云账号和后台遥测。
3. Linux 被当成一等平台，包含 evdev/uinput hook、udev rules、systemd user unit、`.deb`/`.rpm` 打包，避免很多桌面外设软件“只支持 macOS/Windows”的短板。
4. 交互上采用“鼠标示意图 + 可点击热点 + 每键动作选择器”的实体映射设计，让硬件配置不再依赖抽象表单，降低用户心智负担。
5. 支持按应用 profile overlay：当前焦点应用变化时自动切换配置，把硬件快捷键变成上下文感知的生产力入口。

**代码片段或设计模式**

```text
crates/openlogi-gui   # GPUI desktop app
crates/openlogi-cli   # headless inventory / diagnostics
plain TOML config     # diffable, copyable, version-control friendly
```

该项目最值得学习的是“设备控制核心 + 多前端壳层”的模式：核心协议与配置抽象保持稳定，GUI/CLI/系统服务只是不同入口。

**可借鉴点**

Hermes 的本地工具、插件和配置也可采用这种“纯文本配置优先”的路线。对于 Agent 产品而言，用户最怕的是黑箱记忆和不可解释动作；OpenLogi 通过 TOML 让配置可读、可 diff、可迁移，Hermes 的技能启用、权限策略、定时任务和常用工具白名单也可以暴露为可版本化配置。另一个启发是“诊断 CLI”：Hermes 可提供 `hermes doctor tools`、`hermes doctor memory`、`hermes doctor cron` 这类无头诊断入口，方便自动化环境定位问题。

---

## 3. omnigent-ai/omnigent

- GitHub：https://github.com/omnigent-ai/omnigent
- 一句话定位：跨 Claude Code、Codex、Cursor、Pi 与自定义 Agent 的开源元编排框架，提供统一会话、治理、沙箱和多设备协作。
- 领域标签：AI/LLM、Agent 框架、多 Agent、沙箱、治理、协作

**核心架构/技术亮点**

1. “meta-harness” 定位清晰：它不是只做一个 Agent，而是抽象不同 Agent harness 的公共会话层，使 Claude Code、Codex、Pi、自定义 YAML Agent 能在同一任务中组合或互审。
2. 会话可跨终端、浏览器、手机延续，消息、子 Agent、终端、文件保持同步。这把 Agent 从本机 CLI 体验升级成“可远程接管的工作现场”。
3. 支持云沙箱运行，如 Modal、Daytona、Islo 等一次性环境，既减少本机依赖，也为危险命令隔离提供基础。
4. 治理策略可应用于服务器、单个 Agent 或单个聊天：包括高风险动作审批、花费上限、工具访问限制。它把权限控制作为框架一等能力，而不是依赖用户口头提醒。
5. Python 3.12+ 后端结合 Starlette/Uvicorn、MCP、OpenAI SDK、psutil、keyring、CEL 表达式策略等组件，体现出“本地 daemon + Web/API + 策略解释器”的运行时架构。

**代码片段或设计模式**

```toml
[project]
name = "omnigent"
requires-python = ">=3.12"
dependencies = [
  "mcp>=1.0,<2",
  "starlette>=0.27,<1",
  "uvicorn[standard]>=0.30,<1",
  "psutil>=5.9,<8",
  "keyring>=24,<26"
]
```

依赖组合显示它把 Agent runtime、MCP 工具协议、HTTP 服务、宿主进程生命周期、密钥管理整合为一个可托管平台。

**可借鉴点**

Hermes 可以借鉴 Omnigent 的“统一会话总线”思路：用户可能同时使用 CLI、Web、定时任务、IM 或移动端入口，但底层都应映射到同一任务状态、同一工具权限和同一审计日志。尤其是云沙箱与策略层非常适合 Hermes cron 场景：定时任务执行时无人值守，更需要自动拒绝高风险动作、限制成本、记录每个工具调用的可审计原因。多 Agent 互审也可用于代码审查、研究总结、长文写作等高价值流程。

---

## 4. nexu-io/html-video

- GitHub：https://github.com/nexu-io/html-video
- 一句话定位：面向 Coding Agent 的 HTML→视频生成层，把 HTML/CSS/数据/模板渲染成本地 MP4，支持多 Agent 和可插拔渲染引擎。
- 领域标签：Web 项目、程序化视频、Agent Skill、HTML/CSS、Remotion、FFmpeg

**核心架构/技术亮点**

1. 把“视频制作”重新建模为 Web 工程问题：使用 HTML、CSS、GSAP/Hyperframes、模板和数据驱动动画，再通过 headless Chromium + FFmpeg 渲染成真实 MP4。
2. 面向 Agent 的工作流设计：用户可给出 prompt、文章链接或 GitHub repo，Coding Agent 负责填充多帧模板、组织素材、执行渲染，而不是让用户学习专业剪辑软件。
3. 模板库覆盖数据图表、glitch title、液态背景、电影漏光、终端打字、logo outro 等场景；每个模板都是可编辑的单文件 HTML 视频帧，便于复用和版本控制。
4. 渲染引擎采用可插拔路线：当前默认 Hyperframes，计划兼容 Remotion。这避免把产品绑定到单一动画范式。
5. monorepo 使用 pnpm workspace，`packages/*` 与 `templates/*` 分离，说明它把引擎/CLI 与模板资产拆开管理，利于社区贡献模板。

**代码片段或设计模式**

```json
{
  "description": "HTML→Video meta-layer for coding agents",
  "workspaces": ["packages/*", "templates/*"],
  "scripts": {
    "build": "pnpm -r build",
    "smoke": "pnpm --filter @html-video/cli smoke",
    "lint": "biome lint ."
  }
}
```

这是典型的“核心工具包 + 模板包”拆分。模板既是产品能力，也是生态扩展点。

**可借鉴点**

Hermes 的定时学习报告、代码审查摘要、研究卡片等内容可进一步生成“可分享媒体制品”，而不只停留在 Markdown。html-video 展示了一个很实用的 Agent 输出范式：先生成结构化内容，再套模板变成视频/图卡/演示。Hermes 可以设计 `report-to-card`、`summary-to-video` 等技能，把日报、PR Review、论文解读自动转成社交媒体或团队汇报素材。技术上也可采用“模板即代码”的方式，让用户审阅和修改模板，而不是依赖不可控的黑箱生成。

---

## 5. cpaczek/skylight

- GitHub：https://github.com/cpaczek/skylight
- 一句话定位：用 RTL-SDR 接收 ADS-B 信号，把头顶飞过的飞机、星空、卫星实时投影到天花板的 IoT/交互装置。
- 领域标签：嵌入式/IoT、Raspberry Pi、RTL-SDR、实时可视化、Web 控制台、边缘感知

**核心架构/技术亮点**

1. 传感器输入来自本地 RTL-SDR 接收机解码 ADS-B，支持亚秒级本地飞机数据，也能切换到免费 Web API 便于无硬件体验，形成“真实硬件 + 模拟数据源”双路径。
2. 渲染端以 60fps 插值显示飞机位置：由于 ADS-B 约 1Hz 更新，系统通过略微延迟渲染并在真实位置之间 tween，避免飞机在画面上跳变。
3. 可视化不仅是雷达点位，还包含飞机类型感知 glyph、直升机旋翼、螺旋桨动画、高度颜色、彗星尾迹、跑道真实位置、目的地城市与大圆航线。
4. 天空层实时计算太阳、月亮、恒星、星座、行星、ISS/卫星 TLE，并支持从手机控制面板调参、时间跳转和局域网持久配置。
5. 可选 PTZ 天空相机追踪系统融合 ADS-B 预测、传统 blob detector、大目标检测、track-before-detect 和可选 YOLOX-Nano ONNX 语义确认，实现“投影 + 自动拍摄”的闭环。

**代码片段或设计模式**

```json
{
  "scripts": {
    "dev": "concurrently -n server,web,tracker \"pnpm dev:server\" \"pnpm dev:web\" \"pnpm dev:tracker\"",
    "typecheck": "tsc -p shared/tsconfig.json && pnpm -F server typecheck && pnpm -F web typecheck && pnpm -F tracker typecheck"
  }
}
```

它把 `server`、`web`、`tracker`、`shared` 分离，并在开发时并行启动，适合复杂 IoT 系统的多进程调试。

**可借鉴点**

Skylight 对 Hermes 的启发在于“边缘事件流 + 可解释可视化”。很多 Agent 后台任务也像 ADS-B 一样是低频事件流：cron 触发、工具输出、状态更新、异常信号。如果 Hermes 能像 Skylight 一样把事件做时间插值、轨迹化、目标状态可视化，就能让用户更直观看到 Agent 在后台做了什么、为什么转向、是否卡住。另一个可借鉴点是“硬件不可用时保留 Web API fallback”，Hermes 插件也应为外部依赖提供模拟源或降级路径。

---

## 6. OpenBMB/PilotDeck

- GitHub：https://github.com/OpenBMB/PilotDeck
- 一句话定位：以 WorkSpace 为核心的任务型 AI Agent 生产力平台，强调白盒记忆、智能模型路由、Always-on 后台执行与 MCP 原生能力。
- 领域标签：AI/LLM、Agent OS、MCP、记忆系统、生产力平台、Web/CLI/IM

**核心架构/技术亮点**

1. WorkSpace 是系统基本单元：每个项目拥有隔离的文件系统、记忆库和技能集合，避免多项目并行时出现全局上下文污染。
2. 白盒记忆强调可追踪、可编辑：当 Agent 出错时，用户可以定位是哪条记忆影响了判断，并直接修正，而不是只能重开会话。
3. Smart Routing 关注任务成本：不同难度任务自动匹配不同模型，避免所有请求都使用旗舰模型，从而使 Always-on 后台工作更经济。
4. Always-on 机制使 Agent 在用户离开后继续发现值得做的事、汇报进展、把结果落盘。这和一次性聊天助手形成明显差异，更接近“项目操作系统”。
5. 原生支持 MCP，并面向 Web、CLI、IM 多前端保持一致行为；依赖中包含 Playwright MCP、飞书/微信 SDK、PDF/图像处理、token 计算、WebSocket 等，说明其目标是多入口、多模态、多工具统一。

**代码片段或设计模式**

```json
{
  "bin": { "pilotdeck": "./dist/src/cli/pilotdeck.js" },
  "scripts": {
    "prebuild": "node scripts/bootstrap-pilotdeck-config.mjs && cd src/context/memory/edgeclaw-memory-core && npm run build",
    "server": "tsx src/cli/pilotdeck.ts server",
    "skills:migrate": "tsx src/cli/pilotdeck.ts skills migrate"
  },
  "dependencies": ["@modelcontextprotocol/sdk", "edgeclaw-memory-core", "js-tiktoken", "ws", "yaml"]
}
```

构建流程把配置引导、记忆核心构建、CLI server、技能迁移放在一条工程链路里，体现出 Agent 平台对“运行前环境一致性”的重视。

**可借鉴点**

PilotDeck 与 Hermes 的形态很接近，最值得学习的是 WorkSpace 隔离与白盒记忆。Hermes 已有 profile、skills、plugins、cron、memories 等概念，可以进一步把“任务/项目级隔离”做成默认体验：每个长期目标有独立记忆、独立技能集合、独立成本统计和可审计事件日志。Smart Routing 也适合 Hermes：定时学习、摘要整理、文件分类可走便宜模型，安全审查和复杂代码修改再升级到强模型。这样能让长期自动化更可控、更可持续。

---

## 7. arXiv: MemoryWAM — Efficient World Action Modeling with Persistent Memory

- 论文链接：https://arxiv.org/abs/2606.20562v1
- 一句话定位：面向机器人操作的持久记忆 World Action Model，在历史观测与动作建模之间寻找高效推理与长期记忆的平衡。
- 领域标签：arXiv、机器人、世界模型、边缘 AI、长期记忆

**核心架构/技术亮点**

1. 研究问题聚焦真实机器人操作：仅理解当前观测不够，系统还需要历史记忆与动作动态建模，才能在复杂环境中保持鲁棒性。
2. World Action Model 同时建模视觉前瞻和动作序列，条件不仅来自当前观察，也来自历史观察，因此适合长程操作和状态依赖任务。
3. 论文指出现有 WAM 的核心矛盾：高效推理方法通常只使用有限窗口，长期依赖能力不足；而保留大量历史又会显著增加计算成本。
4. Persistent Memory 的方向与 Agent 长期记忆类似：不是把所有历史塞进上下文，而是维护可持续、可检索、可压缩的状态表示。
5. 虽然这是机器人论文，但它反映的趋势对 LLM Agent 也成立：长期任务的关键不只是“大模型推理”，而是“记忆结构如何参与动作决策”。

**代码片段或设计模式**

```text
current observation + persistent memory + dynamics/action model
=> visual foresight + action-conditioned planning
```

该模式可以抽象成“当前输入、长期状态、动作模型”三元组。无论是机器人还是软件 Agent，系统都不应只基于最近消息行动。

**可借鉴点**

Hermes 的 cron 学习、代码修复、资料整理都具备长程任务特征。MemoryWAM 的启发是：不要把历史记录当作被动日志，而要把它升级成动作决策的一部分。例如，Hermes 可以在执行任务前读取“项目长期状态 + 最近失败模式 + 用户偏好 + 当前输入”，再决定工具调用顺序；执行后再把关键状态压缩回持久记忆。这样能避免重复探索、重复犯错，也能让自动任务越跑越懂项目。
