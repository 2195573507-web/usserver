# 2026-06-19 每日学习总结

## 本日最值得关注的 3 个项目

1. **MiMo-Code**（https://github.com/XiaomiMiMo/MiMo-Code）  
   最值得关注的是它把长程代码 Agent 的关键机制做得很完整：多 Agent 模式、SQLite FTS5 持久记忆、自动 checkpoint、上下文重建、树形任务追踪和独立 judge 停止条件。这些都直击自主 Agent 的真实痛点：上下文遗忘、任务漂移、过早结束和难以恢复。

2. **PilotDeck**（https://github.com/OpenBMB/PilotDeck）  
   它把 Agent 产品从“聊天/IDE 辅助”推进到“WorkSpace 操作系统”：每个项目隔离文件、记忆与技能，白盒记忆可追踪可编辑，Smart Routing 控制模型成本，Always-on 支持后台持续产出。这个方向与 Hermes 的 profile、cron、skills、memories 非常契合。

3. **Skylight**（https://github.com/cpaczek/skylight）  
   它不是传统软件工具，而是一个把无线电、实时数据、天文计算、Web 控制台、Raspberry Pi kiosk、可选视觉追踪融合在一起的优秀 IoT 作品。它展示了“边缘事件流 + 可视化反馈 + 手机调参”的产品化方式，对 Agent 运行状态可视化很有启发。

## 跨项目共性技术趋势

- **Agent 平台正在操作系统化**：MiMo-Code、Omnigent、PilotDeck 都不再满足于单轮问答，而是围绕任务、记忆、权限、沙箱、成本、协作与恢复构建完整运行时。
- **记忆从黑箱上下文变成白盒状态层**：项目记忆、checkpoint、workspace memory、persistent memory 都强调可持久、可追踪、可编辑、可压缩。
- **本地优先与可审计配置回潮**：OpenLogi 的 TOML、本地 HID 控制，MiMo/PilotDeck 的文件化任务状态，都说明用户越来越重视可 diff、可迁移、无遥测的控制权。
- **模板即代码成为内容生成新范式**：html-video 把视频生产拆成 HTML/CSS/模板/渲染管线，说明 Agent 输出不必局限文本，可扩展为可版本化媒体制品。
- **边缘与实时系统强调降级路径**：Skylight 同时支持 RTL-SDR 与 Web API，说明优秀系统会为硬件不可用、数据源异常、延迟波动准备 fallback。

## 学习心得

今天的项目共同指向一个趋势：优秀 Agent 产品的核心不只是模型能力，而是长期状态、权限治理、可恢复流程和可信输出。无论是代码助手、Agent OS、桌面硬件工具还是 IoT 装置，真正好用的系统都把复杂性封装在可审计、可配置、可降级的架构里。Hermes 可优先强化白盒记忆、任务级隔离与执行可视化。
