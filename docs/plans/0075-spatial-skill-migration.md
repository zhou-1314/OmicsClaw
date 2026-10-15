# 0075 空间转录组函数库迁移

> 历史迁移方案，所述函数库与步骤执行器已退役。旧交付结果已清理，不作为当前架构的验收依据。

用户确认先迁移空间转录组，再推进其他模态。沿用 0074 的函数库和薄 CLI 方式，保留现有 CLI 的默认参数、数值和输出文件。验收入口为 `load_skill` 的公开函数、CLI，以及示例的 step runner 执行与 replay。起点为 `main` 的 `d70b3225`。

## 范围与顺序

当前 88 个技能中，单细胞 30 个已经完成分类：27 个函数库，3 个保留 CLI。其他域尚有空间 17、bulk RNA-seq 14、基因组 10、蛋白组 8、代谢组 8、文献 1 个。用户在 S1 后确认继续完成其余 16 个空间技能，最后统一合并 main 并推送；实施可按独立批次交给子 agent，最终仍须独立审核。

| 批次 | 技能 |
|---|---|
| S1 | spatial-preprocess |
| S2 | spatial-de、spatial-enrichment、spatial-genes |
| S3 | spatial-annotate、spatial-domains、spatial-microenvironment-subset、spatial-statistics |
| S4 | spatial-integrate、spatial-register、spatial-condition |
| S5 | spatial-deconv、spatial-communication、spatial-cnv、spatial-trajectory、spatial-velocity |
| S6 | spatial-raw-processing |

各批次开始前检查现有算法和依赖，不承诺缺包、数据库或真实输入的后端已经通过验收。文献技能是否适合函数库单独判断，不计作组学模态。

## S1 接口与验证

`preprocess` 接收原始计数 AnnData，返回新的处理后对象，不改调用者的数据。`X` 为 log-normalized 表达，`layers['counts']` 和 `raw` 保留过滤后的计数。公开 `run_info`、簇汇总、QC 表、PCA 方差表及空间分布图；CLI 继续负责读取、报告、画廊和文件输出。

计算复用 `skills/spatial/_lib/preprocessing.py`。目前只有本技能直接调用该模块。补齐随机种子传递，默认 0，与 Scanpy 原来的默认值一致；加载函数库不设置缓存环境变量。组织预设沿用旧行为：等于默认值的阈值仍被预设替换，不能把它描述成“显式传入默认值也能覆盖预设”。

实施次序：

1. 修改算法和 CLI 前，在固定输入上各录两次默认参数及多分辨率变体，保存数值基线。
2. 从公开函数的失败测试开始，实现计算、诊断和返回对象；不复制整套 CLI。
3. CLI 改调函数库，比较旧基线的表、标签、数值列、摘要和图文件名。
4. 增加固定种子的空间合成 demo，明确它不是真实组织测量。示例验证输入不变、计数保存及已知分区的恢复，并运行 replay。
5. 更新生成的 API 文档、空间迁移分类守卫和 CI。独立审核 Standards 与 Spec，修复问题后记录交付证据。

本地 parity 基线位于忽略目录 `tests/parity/golden/`，不进版本库；CI 使用提交的公开行为测试与示例。科学环境为 `/opt/conda/envs/OmicsClaw/bin/python`，另用 CI 的轻依赖环境核验示例。
