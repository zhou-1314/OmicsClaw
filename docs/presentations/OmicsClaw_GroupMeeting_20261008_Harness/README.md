# OmicsClaw 组会汇报：组件机制与设计取舍

参考 Tony Bai《从0开始构建 Agent Harness》本地第 01 至 20 讲，在上一版基础上完善为 45 页：40 页正文、5 页备份，适合约 40 分钟讲解与问答。各组件按问题、机制、当前实现、取舍和验证展开；45 页均含演讲者备注，全文约 10,800 个汉字，可按实际时长选讲。

- [可编辑 PPTX](exports/OmicsClaw_GroupMeeting_20261008_Harness_20261008_030831.pptx)
- [PDF 预览](exports/OmicsClaw_GroupMeeting_20261008_Harness_preview.pdf)
- [逐页讲稿](notes/total.md)
- [教程与源码对应索引](sources/tutorial_alignment.md)
- [部分页面预览](exports/preview.jpg)

## 本轮完善

新增 9 页，分别解释 Thinking 的实现差异、四个基础工具、编辑容错、并发边界、PromptAssembler、PlanInjector、分层错误恢复、动作与副作用评估，以及教程二十讲对应索引。重写 Registry、审查和迁移状态页，其余相关页面同步更新例子、来源和讲稿。

教程中没有被本项目直接采用的方案单独说明：当前 engine 不强制先发一次无工具 Thinking 请求；生产工具调度默认按安全声明分批；计划存储和每轮注入不等于仅靠模型读取 TODO 文件；计划提醒与分层重试也不等于通用死循环检测器或统一恢复模板仓库。没有借用教程中的性能和安全宣传作为项目验证结论。

## 当前项目事实

源码快照为 `90a3bec3e6549bc33dddbf67e4b23869736c49d1`。本次实际运行技能加载器得到 88 个技能、0 skipped，递归核对 83 个函数库和 5 个 CLI-only；分域数量、接口路径与源码哈希在 [repository_snapshot.json](sources/repository_snapshot.json)。XCMS 仅支持合成示例，拒绝真实 raw-MS 输入。

审查流程按当前 OMICSCLAW.md 更新：验证后先展示结果，独立审查默认关闭；用户请求时才审查，Desktop 只为该次新回合授权。请求审查不授权自动修复、重跑或接受模块，接受及跳过审查都需要对应的用户指示。

7,175、7,206 和 41 个示例回放来自计划 0076 的已有验收记录，本次没有重跑全套科学实验或真实模型性能测试。教程原始路径与文件哈希见 [tutorial_manifest.json](sources/tutorial_manifest.json)，没有将教程正文或图片复制进材料包。

## 页面安排

1–5 页介绍问题、范围、地图和请求流；6–19 页展开核心循环、适配器、工具、提示与上下文、记忆计划、提醒和恢复；20–27 页说明会话与入口、权限、沙箱、依赖、扩展、观测和评估；28–36 页连接科学代码、函数库、分析模块、回放、审查、混合语言与已有案例；37–40 页讨论验证证据、当前进展和研究问题；41–45 页是目录、领域、命令、教程对应和来源备份。

## 验证与文件说明

最终 SVG 检查 45 页全部通过，0 errors、0 warnings。ppt-master 原生导出的 postflight 为 passed，无警告。另以 ZIP/XML 和 python-pptx 检查 45 页及 45 份备注，逐页核对全部可见文本与讲稿，文件内包含 801 个原生文字 run、0 个图片 shape。报告见 [delivery_checks.json](validation/delivery_checks.json)。

PPTX 的文字、形状和连线可编辑，字体使用微软雅黑与 Arial，未嵌入字体。PDF 由最终 SVG 通过 CairoSVG 渲染，中文使用本机文泉驿正黑；它是源稿预览，不是 Microsoft PowerPoint 运行时截图。系统缺少 PPT 字体时可能使用替代字体。

上一版 `OmicsClaw_GroupMeeting_20261007` 保留。`svg_output/` 是本版页面源稿，`svg_final/` 是预览，`notes/` 是讲稿，`design_spec.md` 和 `spec_lock.md` 保存设计设置。

## 继续编辑

沿用已安装的 ppt-master 6.6.0，工具依赖在 `/root/.local/share/ppt-master/.venv`，没有修改 OmicsClaw 科学环境。修改 SVG 与对应讲稿后，在项目根目录逐条执行，前一条成功后再执行下一条：

```bash
/root/.local/share/ppt-master/.venv/bin/python /root/.codex/skills/ppt-master/scripts/svg_quality_checker.py docs/presentations/OmicsClaw_GroupMeeting_20261008_Harness --canonical-authoring --stage final --json
/root/.local/share/ppt-master/.venv/bin/python /root/.codex/skills/ppt-master/scripts/total_md_split.py docs/presentations/OmicsClaw_GroupMeeting_20261008_Harness
/root/.local/share/ppt-master/.venv/bin/python /root/.codex/skills/ppt-master/scripts/finalize_svg.py docs/presentations/OmicsClaw_GroupMeeting_20261008_Harness
/root/.local/share/ppt-master/.venv/bin/python /root/.codex/skills/ppt-master/scripts/svg_to_pptx.py docs/presentations/OmicsClaw_GroupMeeting_20261008_Harness
/root/.local/share/ppt-master/.venv/bin/python docs/presentations/OmicsClaw_GroupMeeting_20261008_Harness/scripts/render_preview.py
```
