# 计划 0069：benchmark 的通用 run/grade 基础设施（交付记录）

状态：已实现，待独立审核和 owner 验收（2026-10-08）。分支 `feat/bench-core`，基线 `90a3bec3`，未 push。没有改 `omicsclaw/entry`、`launch`、`engine` 等产品代码，也没有动 `CHANGELOG.md` 和 `README.md`。

## 1. 做了什么

新增 `omicsclaw/bench/`，只含各 suite 共用的部分：读 manifest，给每次运行建隔离工作区，经适配器起一个 agent 进程，记录结局、用量和访问审计，再用确定性 grader 对着工作区外的 oracle 判分。带一个玩具 suite（把一列整数加起来）用于测试和冒烟。具体 suite、数据生成器、LLM judge、compare 报告、多轮会话适配器和外部 agent 适配器都没做；适配器接口留好了，manifest 里写 `module:attribute` 就能接入新的。

bench 只通过 `oc cli` 进程和它写出的文件跟产品打交道，不 import `omicsclaw` 的其他包，`tests/bench/test_bench_is_apart.py` 从源码和 `sys.modules` 两头卡住这一点。

## 2. 目录布局

```text
omicsclaw/bench/
  manifest.py   TOML manifest：case、臂、模型、重复、种子、预算、审计模式；运行顺序
  layout.py     campaign 的目录和 json/jsonl 读写
  stage.py      建工作区，旧尝试改名保留
  process.py    起进程、墙钟、杀进程组、按环境标记清理残留进程
  outcome.py    结局分类
  access.py     事后访问审计
  run.py        续跑、--retry-infra、并发、done.json、predictions/usage.jsonl
  grade.py      grader 接口、正负对照自检、判分前健康检查、grades.jsonl
  adapters/     接口与 omicsclaw 适配器
  example.py    玩具 suite 的 grader 和 case 生成
  __main__.py   python -m omicsclaw.bench
bench/example/  玩具 suite 的 manifest、prompt、权限规则（仓库内，不打包）

<cases>/<case>/public/   拷进工作区        <cases>/<case>/oracle/   只给 grader 读
<out>/cells/<arm>/<model>/<case>/r<k>/     agent 工作区
<out>/meta/<arm>/<model>/<case>/r<k>/      prompt.md staged.json command.json stdout.txt
                                           stderr.txt audit.jsonl access_audit.json done.json
<out>/predictions.jsonl  usage.jsonl  grades.jsonl
```

`<cases>` 和 `<out>` 由命令行给出，放在仓库内或互相嵌套都会被拒绝。

## 3. 怎么运行

```bash
P=/opt/conda/envs/OmicsClaw/bin/python      # 在检出根目录执行，子进程跑的就是这份检出
$P -m omicsclaw.bench example <cases>
$P -m omicsclaw.bench plan  bench/example/manifest.toml
$P -m omicsclaw.bench stage bench/example/manifest.toml --cases <cases> --out <out>
$P -m omicsclaw.bench run   bench/example/manifest.toml --cases <cases> --out <out> \
      [--jobs N] [--retry-infra] [--select 'oc/*/sum-a/r1'] [--env-file <.env>]
$P -m omicsclaw.bench grade bench/example/manifest.toml --cases <cases> --out <out>
```

模型凭据：`oc cli` 自己会读它所在检出根目录的 `.env`，agent 进程另外继承 `run` 的环境；从没有 `.env` 的检出（比如 worktree）跑时用 `--env-file` 补上，文件里的值不覆盖已导出的变量，也不写进任何产物。

退出码：0 表示所看的运行都有属于 agent 的结局；1 表示有运行没跑完、是基础设施失败或没能判分；2 是 manifest、参数或 grader 不可用；130 是被中断。`run` 跳过已有 `done.json` 的运行；`--retry-infra` 只重跑 `infra_failure`，原目录改名为 `r<k>.infra<n>`；上次被打断的运行改名为 `r<k>.incomplete<n>` 后重来。

## 4. 结局分类，以及 `oc cli --prompt-file` 的实测行为

结局七种：`completed`、`no_deliverable`、`approval_denied`、`max_turns`、`truncated`、`timeout`、`infra_failure`。只有最后一种会被重跑、不判分，其余都留在分母里。下表前三列是实测（本机起真实 `oc cli`，后端分别用回环 HTTP 桩和脚本化后端）：

| 情况 | 退出码 | 能看出来的地方 | 记为 |
|---|---|---|---|
| 正常收敛 | 0 | interaction span `agent.stop_reason=converged` | `completed` 或 `no_deliverable` |
| 轮数用尽 | 0 | 只有 `agent.stop_reason=max_turns`，stdout 无提示 | `max_turns` |
| 输出被截断 | 0 | `agent.stop_reason=truncated` | `truncated` |
| provider 500（3 次，44 秒）、401、连不上（216 秒） | 1 | stdout `Failed: ProviderError`，`llm_request` span 带 error | `infra_failure` |
| 子代理里 provider 报错，父代理照样作答 | 0 | 只有 `task` 工具 span 下的 `llm_request` 报错 | `infra_failure` |
| 产品自带的 `OMICSCLAW_TURN_TIMEOUT_S` 到期 | 1 | stdout `Failed: TimeoutError` | `timeout` |
| 墙钟到期，bench 发 SIGTERM | 143 | 0.2 秒内退出，正在跑的 bash 子进程一并结束 | `timeout`；一次模型应答都没有则 `infra_failure` |
| SIGINT / SIGHUP / SIGKILL | 130 / 129 / -9 | SIGKILL 后 bash 子进程存活（它自成会话） | `infra_failure` |
| 命令行被拒 | 2 | stderr 打印用法 | `infra_failure` |
| 审批卡片（危险命令、受保护文件、`default` 模式） | 0 | stdout `Approval required […]` 紧跟 `Approval denied […]: no operator at the terminal` | `approval_denied` |

审批卡片在一次性运行里是立即拒绝，不挂住，stdin 接 `/dev/null` 或一个没人写的管道都一样（实测）。被拒的调用没有工具 span，审计日志里也没有，只在 stdout 里。判定"模型调用失败且没恢复"的规则是：同一个父 span（主循环的一轮，或子代理所在的那次工具调用）下最后一次调用以非取消类错误结束。

## 5. 用量从哪读

读 stderr 上的遥测（`OTEL_ENABLED`、`OTEL_EXPORTER_TYPE=stdout`），对每个 `omicsclaw.llm_request` span 求和，含重试。子代理的调用包含在内，并在 `subagent_llm_calls` 单列；usage 行有 `includes_subagents`、`calls_without_usage`（失败、被取消或后端没报用量的调用数，大于 0 时 token 数是下界）和 `model_resolved`。stdout 的 `Turn N done, tokens:` 行和 interaction span 的合计都不含子代理。真实模型上的一次委派运行：span 合计 7 次调用、70,634 输入 token，其中子代理 3 次；stdout 四行加起来是 62,175。适配器还把合计与进程自己打印的 meter 汇总对一遍，记在 `notes.meter`。

## 6. 由我定的地方，请 owner 裁定

1. manifest 用 TOML（标准库 `tomllib`），没有用 YAML：`omicsclaw/` 里没有模块依赖 PyYAML，核心依赖里也没有它。
2. 模型是 campaign 的一个维度（`[[models]]`），臂也可以自带 `provider`/`model`，那样只跑自己那一个。provider 和 model 留空时交给 agent 自己的环境和 `.env`，实际用了哪个模型记在 `model_resolved`。
3. 启动方式：`<python> -P -c <引导代码> <source_root> cli …`，引导代码把 `source_root` 放到 `sys.path` 最前并在 stderr 打印 `omicsclaw.__file__`。没有用 `PYTHONPATH`，因为它会传给 agent 的 bash 子进程。`python` 和 `source_root` 在臂的 `options` 里可配。import 到别的检出记为 `infra_failure: source_mismatch`。
4. 默认给 `oc cli` 的设置：`OMICSCLAW_PERMISSION_MODE=auto-approve`、`OMICSCLAW_SKILLS_DIR=<source_root>/skills`、`OMICSCLAW_MAX_TURNS` 取预算值；`OMICSCLAW_SKILL_ENV` 保持产品默认，没有设成 `off`。臂的 `env` 可以覆盖这些，但不能改审计日志和遥测那几项。没有设 `OMICSCLAW_APPROVAL_TIMEOUT_S`，因为实测卡片不会挂住。
5. 打开了 `OMICSCLAW_OTEL_CAPTURE_CONTENT`，访问审计靠它拿到工具参数（产品的审计日志只记参数摘要）。
6. `approval_denied` 单列为一种结局，有交付物时照常判分。
7. 没有交付物的运行不交给 grader，`grades.jsonl` 里留一行 `graded=false, score=null`，算不算 0 分留给以后的 compare。超时或轮数用尽但交付物存在的运行照常判分，行里保留 `outcome`。
8. grader 的正负对照由 grader 自己用代码给出（`controls()`），自检不过时整个 `grade` 中止、什么都不写。
9. 判分前健康检查是按运行做的：重读证据、重新分类，和 `done.json` 记的不一致就不判分，其余运行照判。没有做成"整轮作废"。
10. stage 时顺手建好交付物的父目录（如 `output/`）；`public/` 里的符号链接按文件拷贝。
11. 运行顺序：按 (case, 重复) 分块后用种子打乱，块内各臂各模型再打乱。
12. 没有记录 manifest 的哈希，续跑时不检查 manifest 是否被改过。

## 7. 防线账

| 防线 | 防什么 | 更宽的旁路 | 值不值 |
|---|---|---|---|
| oracle 放工作区外，工作区只有 `public/` 的拷贝 | agent 顺手读到真值 | `bash` 能读整个文件系统；`OMICSCLAW_AUDIT_LOG` 等环境变量会暴露 `<out>` 的位置 | 值，零成本 |
| `meta/` 放工作区外 | agent 无意中改写自己的日志和 prompt | 同上，`bash` 能写任何地方 | 值，零成本 |
| 事后访问审计，只记录并打标 | 记下提到 cases 根、meta 根和 manifest 模式（如 `pip install`）的命令、审计日志行和工作区文本文件 | 运行时拼出来的路径、遍历目录读到的文件、4096 字节之后的参数都看不到 | 值，约 160 行，不拦截 |
| 墙钟到期杀进程组，结束后按环境标记清理残留 | 运行挂死；agent 起的后台进程在收集之后继续写工作区 | 清掉自己环境变量的进程（`env -i`）逃得掉 | 值；后一半实测必要，见 §4 的 SIGKILL 一行，正常退出后 `nohup setsid` 起的进程也会留下 |
| 权限规则文件（臂的 `permission_rules`，示例里拒绝 `web_search`、`web_fetch`） | 最省事的联网途径 | `bash` 里的 `curl`、`pip` | 可选，一行配置，靠审计模式发现旁路 |

没有加容器、网络隔离、指纹、签名或任何拦截。

## 8. 验证证据

- `rapids_singlecell` 的 python 跑 `-m pytest -q -p no:randomly tests/bench`：151 passed，22 秒；`OmicsClaw` 环境（Python 3.11，editable 安装指向主检出）下同样 151 passed。其中 `test_oc_cli_contract.py` 起 9 个真实 `oc cli` 进程（脚本化后端），`test_example_suite.py` 用仓库里的 manifest 走完四个子命令。
- `tests/launch/test_grammar.py` 和分层守卫共 6 个文件：211 passed，1 skipped，29 秒（`MODULE_GUARDS` 登记了 `bench/__main__.py`；登记前这个测试是红的）。顶层 `tests/test_*.py`：224 passed，8 skipped，1 deselected，1 xpassed，36 秒；xpassed 是 `tests/ci_known_failures.txt` 里标了 `env` 的那条，与本分支无关。
- 变异验证 23 处，逐个改坏、跑对应测试、还原并确认工作树干净，全部变红：续跑、`--retry-infra`（两个方向）、改名保留、只拷 `public/`、`meta/` 位置、基础设施失败优先级、非零退出、子代理报错、用量含子代理、停止原因、审批卡片、源码根、审计打标（两处）、自检（三处）、健康检查、基础设施失败不判分、残留进程清理、stdin、各臂交错。第一轮"源码根"那条没红，原因是测试用的后端垫片自己改了 `sys.path`，已修。
- 真实冒烟（deepseek-v4-flash，凭据经 `--env-file` 传入）。产物在 `/tmp/claude-0/-workspace-dataset-private-zhouwg-data-OmicsClaw/c9ac411b-8fa2-4e24-a998-5f5f8f957642/scratchpad/bench-smoke/runs/`：`smoke3` 是仓库里的示例 manifest 在 `57686cb0` 上走 stage、run、grade，两次运行都 `completed` 并判为通过，各 4 次模型调用，输入 61,538 和 61,605 token（缓存 56,832），输出 226 和 349，墙钟 16.9 秒；`smoke2` 先用错误的 key 跑，真实后端返回 401，记为 `infra_failure`（4.2 秒），再 `--retry-infra` 得到 `completed`（43.6 秒，即 §5 那次委派运行），第一次尝试留在 `r1.infra1`。84 个产物文件里没有搜到 API key。

## 9. 产品侧缺口（没改，列给 owner）

1. `oc cli --prompt-file` 在轮数用尽和输出截断时退出码是 0，stdout 也没有提示。
2. 子代理的模型调用因 provider 报错而失败时，父代理拿到一条工具错误后可以继续作答，进程以 0 退出。
3. 子代理的用量不并入父会话：`report_usage` 在生产代码里没有接收方，stdout 的 token 行和 interaction span 都不含它。
4. 被审批或规则拒掉的工具调用没有工具 span，也不进 `OMICSCLAW_AUDIT_LOG`。
5. 审计日志只记参数摘要，拿不到命令文本；要看命令只能打开遥测的内容捕获，而它连工具输出一起记，每项截到 4096 字节。
6. `bash` 工具把进程环境（含 `LLM_API_KEY`）原样传给子进程。agent 一旦打印环境，key 会进会话库，也会进遥测和 stdout 记录。
7. stdout 在非终端下按 80 列折行，长行不适合机器解析。

## 10. 已知限制和没验证的部分

- 上面第 6 条意味着 `meta/stdout.txt`、`meta/stderr.txt` 可能带上 key。bench 自己不写任何继承来的环境变量值（`command.json` 只记名字），但没有对这两个文件做脱敏；要不要加一步事后替换请 owner 定。
- 墙钟超时里，模型端在中途卡死的情况仍记为 `timeout`，只有"一次应答都没有"才记为基础设施失败。压缩用的摘要调用失败后同一轮主调用成功，不会被判为基础设施失败，只体现在 `llm_errors`。
- 残留进程清理依赖 `/proc`，只在 Linux 上生效；整个 harness 只在 POSIX 上可用。
- 真实模型上只完成了 5 次运行（另有 1 次被 401 拒绝），都是 deepseek，并发只试过 2；真实后端上没有触发过审批卡片、轮数用尽和墙钟超时，这三种只用脚本化后端在真实 `oc cli` 进程上验证过。CI 上的 Eval workflow 没有实际跑过。
- 没有做成本折算、推理 token、模型快照文件和 manifest 冻结检查。
