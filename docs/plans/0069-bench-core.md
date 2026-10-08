# 计划 0069：benchmark 的通用 run/grade 基础设施（交付记录）

状态：已实现，独立审核"有条件通过"后做完一轮修复，待复核和 owner 验收（2026-10-08）。分支 `feat/bench-core`，基线 `90a3bec3`，未 push。没有改 `omicsclaw/entry`、`launch`、`engine` 等产品代码，也没有动 `CHANGELOG.md` 和 `README.md`。

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
  run.py        续跑、--retry-infra、并发、输出目录锁、done.json 和各 jsonl
  grade.py      grader 接口、正负对照自检、判分前健康检查、grades.jsonl
  adapters/     接口与 omicsclaw 适配器
  example.py    玩具 suite 的 grader 和 case 生成
  __main__.py   python -m omicsclaw.bench
bench/example/  玩具 suite 的 manifest、prompt、权限规则（仓库内，不打包）

<cases>/<case>/public/   拷进工作区        <cases>/<case>/oracle/   只给 grader 读
<out>/cells/<arm>/<model>/<case>/r<k>/     agent 工作区
<out>/meta/<arm>/<model>/<case>/r<k>/      prompt.md staged.json command.json stdout.txt
                                           stderr.txt audit.jsonl access_audit.json done.json
<out>/predictions.jsonl usage.jsonl grades.jsonl   每次运行一行
<out>/attempts.jsonl                                每个启动过的尝试一行，含被改名保留的
<out>/.bench.lock                                   stage 和 run 持有的锁
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

模型凭据：`oc cli` 自己会读它所在检出根目录的 `.env`，agent 进程另外继承 `run` 的环境；从没有 `.env` 的检出（比如 worktree）跑时用 `--env-file` 补上。这个文件按产品读 `.env` 的方式解析（`export`、引号、值后面的 ` # 注释`），不展开 `${VAR}`，不支持跨行的值；文件里的值不覆盖已导出的变量，也不写进任何产物。

退出码：0 表示所看的运行都有属于 agent 的结局；1 表示有运行没跑完、是基础设施失败或没能判分；2 是 manifest、参数或 grader 不可用，或输出目录正被另一个 `stage`/`run` 占用；130、143、129 分别是被 Ctrl-C、SIGTERM、SIGHUP 停下，三种情况下在跑的 agent 都会被杀掉、该运行留作未完成。`run` 跳过已有 `done.json` 的运行；`--retry-infra` 只重跑 `infra_failure`，原目录改名为 `r<k>.infra<n>`；上次被打断的运行改名为 `r<k>.incomplete<n>` 后重来。

## 4. 结局分类，以及 `oc cli --prompt-file` 的实测行为

结局七种：`completed`、`no_deliverable`、`approval_denied`、`max_turns`、`truncated`、`timeout`、`infra_failure`。只有最后一种会被重跑、不判分，其余都留在分母里。下表前三列是实测（本机起真实 `oc cli`，后端分别用回环 HTTP 桩和脚本化后端）：

| 情况 | 退出码 | 能看出来的地方 | 记为 |
|---|---|---|---|
| 正常收敛 | 0 | interaction span `agent.stop_reason=converged` | `completed` 或 `no_deliverable` |
| 轮数用尽 | 0 | 只有 `agent.stop_reason=max_turns`，stdout 无提示 | `max_turns` |
| 输出被截断 | 0 | `agent.stop_reason=truncated` | `truncated` |
| provider 500（3 次，44 秒）、401、连不上（216 秒） | 1 | stdout `Failed: ProviderError`，`llm_request` span 带 error | `infra_failure` |
| 子代理里 provider 报错，父代理照样作答 | 0 | 只有 `task` 工具 span 下的 `llm_request` 报错 | `infra_failure` |
| 200 带错误体，或 SSE 发一个 chunk 后断开 | 0 | 那次调用的 span 没有 error，输入 token 是 0 | `infra_failure: provider_error: empty_response` |
| 某次调用 500，重试还在途时墙钟到期 | 143 | 同一轮里报错的 span 后面跟着一个被取消的 span | `infra_failure` |
| 墙钟到期，或产品自带的 `OMICSCLAW_TURN_TIMEOUT_S` 到期（退出码 1，stdout `Failed: TimeoutError`） | 143 / 1 | 墙钟下 0.2 秒内退出，正在跑的 bash 子进程一并结束 | `timeout`；一次模型应答都没有则 `infra_failure` |
| SIGINT / SIGHUP / SIGKILL | 130 / 129 / -9 | SIGKILL 后 bash 子进程存活（它自成会话） | `infra_failure` |
| 命令行被拒 | 2 | stderr 打印用法 | `infra_failure` |
| 审批卡片（危险命令、受保护文件、`default` 模式） | 0 | stdout `Approval required […]` 紧跟 `Approval denied […]: no operator at the terminal` | `approval_denied` |

审批卡片在一次性运行里是立即拒绝，不挂住，stdin 接 `/dev/null` 或一个没人写的管道都一样（实测）。被拒的调用没有工具 span，审计日志里也没有，只在 stdout 里。

"模型调用没得到应答且没恢复"按父 span 判（主循环的一轮，或子代理所在的那次工具调用）：去掉因为运行被停下而取消的调用，看剩下的最后一次。它以错误结束记 `provider_error: <Error>`；没有错误但没报输入 token 记 `empty_response`，因为后端只要应答就读过 prompt。报了用量而内容为空的应答不算基础设施失败：模型要是稳定返回空，这种运行会被反复重跑、永远进不了分母。由此带来一个后果：不报用量的后端，每次运行都会被记成 `empty_response`，目前不能用于 campaign。

## 5. 用量从哪读

读 stderr 上的遥测（`OTEL_ENABLED`、`OTEL_EXPORTER_TYPE=stdout`），对每个 `omicsclaw.llm_request` span 求和。子代理的调用包含在内，并在 `subagent_llm_calls` 单列。`llm_calls` 含引擎层的重试；SDK 内部的重试（`OMICSCLAW_LLM_MAX_RETRIES`，缺省 5 次）不产生 span，不在其中。`calls_without_usage` 是没报输入 token 的调用数（失败、被取消、空应答或后端不报用量），它们不进合计，大于 0 时合计是下界。stdout 的 `Turn N done, tokens:` 行和 interaction span 的合计都不含子代理。真实模型上的一次委派运行：span 合计 7 次调用、70,634 输入 token，其中子代理 3 次；stdout 四行加起来是 62,175。适配器还把合计与进程自己打印的 meter 汇总对一遍，记在 `notes.meter`。

`usage.jsonl` 每次运行一行，只有当前这次尝试。campaign 的总花费从 `attempts.jsonl` 加：被改名保留的尝试也各占一行；没留下 `done.json` 的尝试记为 `incomplete`、用量为空，有这种行时总数是下界。

## 6. 由我定的地方，请 owner 裁定

1. manifest 用 TOML（标准库 `tomllib`），没有用 YAML：`omicsclaw/` 里没有模块依赖 PyYAML，核心依赖里也没有它。
2. 模型是 campaign 的一个维度（`[[models]]`），臂也可以自带 `provider`/`model`，那样只跑自己那一个。两者留空时交给 agent 自己的环境和 `.env`，请求的模型名记在 `model_resolved`。
3. 启动方式：`<python> -P -c <引导代码> <source_root> cli …`，引导代码把 `source_root` 放到 `sys.path` 最前并在 stderr 打印 `omicsclaw.__file__`。没有用 `PYTHONPATH`，因为它会传给 agent 的 bash 子进程。`python` 和 `source_root` 在臂的 `options` 里可配。import 到别的检出记为 `infra_failure: source_mismatch`。
4. 每次启动把 `source_root`、它的 git commit、`omicsclaw/` 与 `skills/` 是否有未提交改动写进 `command.json` 和 `done.json` 的 `agent_code`。判分时的健康检查拿这份记录里的 `source_root` 比对，所以结果可以拷到别处或在原检出删掉之后判分。
5. 默认给 `oc cli` 的设置：`OMICSCLAW_PERMISSION_MODE=auto-approve`、`OMICSCLAW_SKILLS_DIR=<source_root>/skills`、`OMICSCLAW_MAX_TURNS` 取预算值；`OMICSCLAW_SKILL_ENV` 保持产品默认。臂的 `env` 可以覆盖这些，但不能改审计日志和遥测那几项。没有设 `OMICSCLAW_APPROVAL_TIMEOUT_S`，因为实测卡片不会挂住。
6. 打开了 `OMICSCLAW_OTEL_CAPTURE_CONTENT`，访问审计靠它拿到工具参数（产品的审计日志只记参数摘要）。关掉时审计记 `commands_scanned: null`，表示命令没扫，区别于扫过而没有命中。
7. `approval_denied` 单列为一种结局，有交付物时照常判分。
8. 没有交付物的运行不交给 grader，`grades.jsonl` 里留一行 `graded=false, score=null`，算不算 0 分留给以后的 compare。超时或轮数用尽但交付物存在的运行照常判分，行里保留 `outcome`。
9. grader 的正负对照由 grader 自己用代码给出（`controls()`），自检不过时整个 `grade` 中止、什么都不写。
10. 判分前健康检查是按运行做的：重读证据、重新分类，和 `done.json` 记的不一致就不判分，其余运行照判。没有做成"整轮作废"。
11. stage 时顺手建好交付物的父目录（如 `output/`）；`public/` 里的符号链接按目标内容拷贝，目标不存在时报 `StageError`。
12. 运行顺序：按 (case, 重复) 分块后用种子打乱，块内各臂各模型再打乱。
13. 同一个 `<out>` 同时只允许一个 `stage` 或 `run`，靠 `<out>/.bench.lock` 上的 `flock`，第二个直接报错退出。
14. stdout 里行首的 `Failed: <Error>` 只在退出码是 1 时采信。
15. 没有记录 manifest 的哈希，续跑时不检查 manifest 是否被改过。

## 7. 防线账

| 防线 | 防什么 | 更宽的旁路 | 值不值 |
|---|---|---|---|
| oracle 放工作区外，工作区只有 `public/` 的拷贝 | agent 顺手读到真值 | `bash` 能读整个文件系统；`OMICSCLAW_AUDIT_LOG` 等环境变量会暴露 `<out>` 的位置 | 值，零成本 |
| `meta/` 放工作区外 | agent 无意中改写自己的日志和 prompt | 同上，`bash` 能写任何地方 | 值，零成本 |
| 事后访问审计，只记录并打标 | 命令、审计日志行和工作区文本文件里出现：cases 根；`<out>` 下自己工作区以外的路径（别的臂、被保留的旧尝试、`meta/`、结果文件）；用 `../` 爬出工作区的相对路径；manifest 里的模式（如 `pip install`） | 运行时拼出来的路径（`find /`、`$VAR` 展开）、遍历目录读到的文件、4096 字节之后的参数都看不到。先 `cd` 进子目录再用 `..` 的命令会被误记 | 值，约 250 行，不拦截 |
| 墙钟到期杀进程组，结束后按环境标记清理残留 | 运行挂死；agent 起的后台进程在收集之后继续写工作区 | 清掉自己环境变量的进程（`env -i`）逃得掉 | 值；后一半实测必要，见 §4 的 SIGKILL 一行，正常退出后 `nohup setsid` 起的进程也会留下 |
| 权限规则文件（臂的 `permission_rules`，示例里拒绝 `web_search`、`web_fetch`） | 最省事的联网途径 | `bash` 里的 `curl`、`pip` | 可选，一行配置，靠审计模式发现旁路 |

没有加容器、网络隔离、指纹、签名或任何拦截。同级工作区和 `<out>` 根对 agent 仍然可见，布局没有改，只是访问会被记下来。示例 grader 不把真值写进 `grades.jsonl`（它在 `<out>` 根，重试的 agent 读得到）；以后的 grader 也要守这一条。写 case 时注意：`public/` 里的链接指向 `oracle/` 等于把真值放进工作区，harness 不检查这个。

## 8. 验证证据

- `rapids_singlecell` 的 python 跑 `-m pytest -q -p no:randomly tests/bench`：214 passed，31 秒；`OmicsClaw` 环境（Python 3.11，editable 安装指向主检出）下同样 214 passed。其中 `test_oc_cli_contract.py` 起 13 个真实 `oc cli` 进程（脚本化后端），`test_example_suite.py` 用仓库里的 manifest 走完四个子命令。
- `tests/launch/test_grammar.py` 和分层守卫共 6 个文件：211 passed，1 skipped，28 秒（`MODULE_GUARDS` 登记了 `bench/__main__.py`；登记前这个测试是红的）。顶层 `tests/test_*.py` 只在修复前跑过：224 passed，8 skipped，1 deselected，1 xpassed。
- 变异验证：第一轮 23 处；修复这一轮 29 处，含审核方发现没红的 5 处和这一轮每条新规则各一处，全部变红，每次还原后工作树干净。清理残留进程的隔离性另有一条测试，用带别的运行标记的进程和不带标记的进程各一个做旁观者，没有靠改坏清理逻辑去验证。
- 审核方的回环 HTTP 桩（走真实的 provider 适配器）在修复后重跑：200 带错误体、流中断、写了草稿后空应答都记为 `empty_response`；500 后重试在途超时记为 `provider_error`；纯卡死仍是 `timeout`；产品回合超时且零应答记为 `timeout_before_any_model_response`。
- 真实冒烟（deepseek-v4-flash，凭据经 `--env-file` 传入，产物留在本机、没有入库）。修复后在 `f359dd11` 上用仓库里的示例 manifest 走 run、grade：两次运行都 `completed` 并判为通过，各 4 次模型调用，输入 61,450 和 61,655 token（缓存 56,832 和 56,960），输出 238 和 415，墙钟 41.0 和 25.4 秒，访问审计各扫 3 条命令、没有命中；把结果拷到另一路径、从一份导出的代码树判分，两行都通过健康检查。修复前还跑过：先用错误的 key，真实后端返回 401，记为 `infra_failure`（4.2 秒），再 `--retry-infra` 得到 `completed`（43.6 秒，即 §5 那次委派运行），第一次尝试留在 `r1.infra1`。全部产物文件里没有搜到 API key。

## 9. 产品侧缺口（没改，列给 owner）

1. `oc cli --prompt-file` 在轮数用尽和输出截断时退出码是 0，stdout 也没有提示。
2. 子代理的模型调用因 provider 报错而失败时，父代理拿到一条工具错误后可以继续作答，进程以 0 退出。
3. 子代理的用量不并入父会话：`report_usage` 在生产代码里没有接收方，stdout 的 token 行和 interaction span 都不含它。
4. 被审批或规则拒掉的工具调用没有工具 span，也不进 `OMICSCLAW_AUDIT_LOG`。
5. 审计日志只记参数摘要，拿不到命令文本；要看命令只能打开遥测的内容捕获，而它连工具输出一起记，每项截到 4096 字节。
6. `bash` 工具把进程环境（含 `LLM_API_KEY`）原样传给子进程。agent 一旦打印环境，key 会进会话库，也会进遥测和 stdout 记录。
7. stdout 在非终端下按 80 列折行，长行不适合机器解析。
8. OpenAI 兼容适配器的流在没有 `finish_reason` 时仍然正常结束：200 带错误体或流中途断开，到引擎那里是一次没有内容、用量为 0 的应答，不抛 `ProviderError`。

## 10. 已知限制和没验证的部分

- 上面第 6 条意味着 `meta/stdout.txt`、`meta/stderr.txt` 可能带上 key。bench 自己不写任何继承来的环境变量值（`command.json` 只记名字），但没有对这两个文件做脱敏；要不要加一步事后替换请 owner 定。
- stdout 转写是按行首文字认的，agent 自己的回答可以撞上：行首出现 `Approval required [` 会被数成一张卡片；退出码恰好是 1 时，行首的 `Failed: TimeoutError` 会被当成产品的回合超时。这一轮只记录，没有修。
- 墙钟超时里，模型端在中途卡死而之前没有报过错的情况仍记为 `timeout`。压缩用的摘要调用失败后同一轮主调用成功，不会被判为基础设施失败，只体现在 `llm_errors`。
- `model_resolved` 是发给 provider 的模型名，不是 provider 返回的快照名。Anthropic 系的 cache 写入 token 拿不到，span 上只有 cache 读取。没有推理 token、成本折算和 manifest 冻结检查。
- 每完成一次运行就重读全部 `done.json` 来重写各 jsonl，运行数上千时这一步会变慢。
- 残留进程清理依赖 `/proc`，只在 Linux 上生效；整个 harness 只在 POSIX 上可用。
- 真实模型上只完成了 7 次运行（另有 1 次被 401 拒绝），都是 deepseek，并发只试过 2；真实后端上没有触发过审批卡片、轮数用尽和墙钟超时，这三种只用脚本化后端在真实 `oc cli` 进程上验证过。
