# RET-04b Ask Codex 独立审查记录

日期：2026-07-13

范围：显式 skill 统一执行门（shared runner、agent、pipeline、sync/async）

审查方式：Ask Codex 独立只读复核；审查器不修改、提交或推送文件

结论：**PASS — 当前范围内无 Blocker / High**

本轮只使用 Ask Codex 做独立交叉审查，没有使用 Claude Code。审查不是形式化确认：每一轮
都要求从公开 runner/registry 入口重新构造反例，发现由主实现方以红测复现后才整改。

## 审查过程

### 第一轮：NOT PASS

首轮发现显式门对外部未标注 H5AD、目录输入与 non-H5AD 过严或过松，多输入缺失路径可
绕过；pipeline 还会在首步门禁前创建输出目录，并丢失子步骤 stderr。

整改后，显式选择只放宽无法观测的 modality/data-shape，不放宽已观测冲突；non-H5AD
仍检查存在性、类型、env/config；pipeline 在 composite 输出分配前检查首步并保留失败诊断。

### 第二轮：NOT PASS

第二轮复现 non-H5AD 路径因提前返回而跳过 env/config、带已知后缀的自然语言被误判本地
文件、坏 session 直接抛异常，以及 agent pipeline 在门禁前 reserve Run。

整改后，evaluator 用显式 evidence flags 区分“不可观测”与“无需检查”；自然语言、DOI、
URL 与本地路径分流；坏 session 返回结构化失败；普通 skill 与 pipeline 均在 reserve 前预检。

### 第三轮：NOT PASS

第三轮发现 `path_kinds` 只实现 file-only→directory 的单向拒绝，directory-only 仍接受文件，
file-only 仍接受 freeform；目录能力迁移不完整；合法空 session 仍会先创建输出再失败。

整改引入 `file | directory | freeform` 对称矩阵、空 session fail-closed、无输入提前返回，并对
真实目录消费者开展代码与文档审计。

### 第四轮：扩大全库攻击面后 PASS

最后一轮没有止于已知样例，而是核对 95 个入口的公开输入行为与 machine contract，陆续
发现并关闭以下系统性问题：

- 12 个真实目录消费者缺少或需要校准 `path_kinds`，包括 scATAC、10x、Visium/Xenium、
  FASTQ、typed consensus run 等路径。
- `.txt` gene list、`.fq`、`.h5/.hdf5`、`.zarr/`、spatial raw JSON/YAML config、loom/CSV/TSV
  等真实公开格式未完整进入 `file_types`。
- 普通点号目录（如 `sample.v1/`）被误作文件类型；修复后 `InputProfile.path_kind` 成为 routing
  与 execution 的共同事实，只有已知 suffix-typed directory format（当前 `.zarr/`）保留类型。
- 未声明 zarr 的 10x-only skill 仍会 hard block；声明 zarr 的 Xenium consumer 可通过类型门。
- 目录输入不再调用仅适用于普通文件的 `sha256_file()`。

最终 Ask Codex 明确给出 PASS：无 Blocker / High，可结束 RET-04b 并进入 RET-05。

## 本地复验证据

- runner/precondition/schema/SKILL.md/registry/agent/pipeline/interactive 定向回归：
  **169 passed**。
- Ask Codex 独立集合：**165 passed / 3 skipped / 2 xfailed / 3 xpassed**。
- `skill.yaml`：**95 valid / 0 invalid**；`skill_lint.py --all` 通过。
- `generate_skill_md.py --all --check`、`generate_catalog.py --check`、
  `generate_parameters_md.py --all --check` 全部通过。
- `git diff --check` 通过。

## 明确边界与后续项

本次 PASS 证明“显式执行入口统一经过可观测前置条件门”，不表示所有输入内容均已验证：

- H5AD 有 backed metadata 探针；CSV/FASTQ/PDF 与普通目录目前只做存在性、kind/type 和
  env/config 检查，损坏内容或错误 10x 布局仍由 skill 自身拒绝。
- 压缩能力尚未进入 machine contract；例如后缀归一后的 `.sam.gz`/`.fasta.gz` 可能通过类型门，
  但使用普通文本 `open()` 的消费者不一定能读取。该项在 RET-05 后单独建模。
- 检查与 subprocess 打开文件之间仍有 OS 级 TOCTOU 窗口；执行门已禁用 metadata cache，
  但不声称提供文件描述符级原子绑定。
- `config` 的通用观测来源尚未定义；任何声明 config 前置条件的技能仍会 fail closed。
- compatibility DAG、候选级 penalty 与复合 topo chain 属于 RET-05，不由本次 PASS 外推。
