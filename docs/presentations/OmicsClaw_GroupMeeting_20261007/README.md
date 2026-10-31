# OmicsClaw 组会汇报：架构与设计

中文、16:9，共 36 页：32 页正文和 4 页备份，适合约 30–40 分钟组会。讲稿约 7,900 个汉字，已嵌入每页演讲者备注；组件之间的关系用可编辑文字、矩形、连线和箭头表达。

## 交付文件

- [可编辑 PPTX](exports/OmicsClaw_GroupMeeting_20261007_094341.pptx)
- [PDF 预览](exports/OmicsClaw_GroupMeeting_20261007_preview.pdf)
- [逐页讲稿](notes/total.md)
- [部分页面预览](exports/preview.jpg)
- [事实核对与源码入口](sources/verified_facts.md)
- [代码快照、技能清单与文件哈希](sources/repository_snapshot.json)

PDF 由最终 SVG 渲染，中文字体使用本机的文泉驿正黑；它是源稿预览，不是 PowerPoint 实际渲染的截图。PPTX 的字体为微软雅黑和 Arial，未嵌入字体；在缺少这些字体的系统打开时可能发生替换。

## 内容组织

第 1–5 页介绍问题、范围、系统地图和请求流；第 6–14 页解释 schema、provider、engine、tools、context、memory、planning、skills 以及函数库边界；第 15–18 页展开分析模块、回放、审查、验收和 Python/R 协作；第 19–26 页说明权限、隔离、依赖、扩展、观测、组合根、入口和部署；第 27–32 页讨论已有验收案例、可靠性、验证口径、迁移状态和下一步。第 33–36 页用于查阅组件、领域、命令和来源。

## 版本和事实范围

架构与接口统计固定于制作开始时的干净代码快照 `43e9c034b1f04773aa2121d9aab0944beec3ec57`，分支为 `feat/spatial-skill-migration`。当时实际加载 88 个技能、0 skipped，函数库为单细胞 27 个和空间 1 个。

制作期间其他会话推进了空间 S2–S6，第 30 页补充“实施与验收中”的状态。后续 16 个技能不被计入本次已验收函数库数量，S6 原始处理仍保留 CLI。说明保存在 [并行变更注记](sources/concurrent_work_note.txt)。本次未改动其他会话的源码。

7,024、846、88 和 1,242 等测试数字来自已保存的交付记录，本次没有重跑相应全套测试或科学实验。PPT 区分接口检查、行为回归、真实运行和科学有效性，不把目录覆盖或一次示例成功解释为所有后端已经验证。

## 安装与复用

已安装 `hugohe3/ppt-master` 6.6.0，提交 `2d72da616cf9fa40d4dcaf59fd4c980ecf534b7d`。

- 完整工具仓库：`/root/.local/share/ppt-master`
- Codex skill：`/root/.codex/skills/ppt-master`
- 独立解释器：`/root/.local/share/ppt-master/.venv/bin/python`

重启 Codex 后可以在新会话调用安装的 skill。工具依赖在专用 venv 中，没有安装到 OmicsClaw 科学环境。安装检查和制作状态保存在 `installation.json`。

`svg_output/` 是可编辑源稿，`svg_final/` 是自包含预览；`design_spec.md` 和 `spec_lock.md` 保留页面规划与设计设置。用户已明确授权自行决定后续设置并连续完成，因此没有创建虚构的网页确认结果。

编辑后按顺序执行下面的命令，每条成功后再执行下一条。示例中的项目路径对应本机工作区。

```bash
/root/.local/share/ppt-master/.venv/bin/python /root/.local/share/ppt-master/skills/ppt-master/scripts/svg_quality_checker.py /workspace/dataset/private/zhouwg_data/OmicsClaw/docs/presentations/OmicsClaw_GroupMeeting_20261007 --canonical-authoring --stage final --json
/root/.local/share/ppt-master/.venv/bin/python /root/.local/share/ppt-master/skills/ppt-master/scripts/total_md_split.py /workspace/dataset/private/zhouwg_data/OmicsClaw/docs/presentations/OmicsClaw_GroupMeeting_20261007
/root/.local/share/ppt-master/.venv/bin/python /root/.local/share/ppt-master/skills/ppt-master/scripts/finalize_svg.py /workspace/dataset/private/zhouwg_data/OmicsClaw/docs/presentations/OmicsClaw_GroupMeeting_20261007
/root/.local/share/ppt-master/.venv/bin/python /root/.local/share/ppt-master/skills/ppt-master/scripts/svg_to_pptx.py /workspace/dataset/private/zhouwg_data/OmicsClaw/docs/presentations/OmicsClaw_GroupMeeting_20261007
/root/.local/share/ppt-master/.venv/bin/python /workspace/dataset/private/zhouwg_data/OmicsClaw/docs/presentations/OmicsClaw_GroupMeeting_20261007/scripts/render_preview.py
```

## 验证

最终 SVG 检查为 36 页、0 errors、0 warnings；PPTX 导出后检查为 `passed`，无警告。另用 ZIP/XML 与 python-pptx 核验 36 页、36 份备注、631 个原生文字 run、0 个整页图片元素，并核对讲稿与页码一一对应。检查报告和实际执行范围保存在 `validation/`。本机未用 Microsoft PowerPoint 渲染；PDF 使用上述 SVG 预览路径。
