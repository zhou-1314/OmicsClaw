模块命令由 skills/_sdk/notebook/run.py 提供。new qc 创建模块与策略模板，run 执行过期步骤，status 显示状态和原因，replay 完整重跑并把 validate 放在最后。accept 配合当前 review 文件，在用户明确同意后接受并冻结模块。

独立审查默认关闭，不能因此自动认为结果已获批准。用户明确接受且明确要求跳过审查时，才使用 --skip-review 并保存用户的理由。已接受模块要修改，应先 revise 并保留 baseline。

执行器在前台运行，以取得完整输出与失败信息；从实际分析工作区调用时，用完整的仓库执行器路径或已配置入口。oc 仍是 cli、desktop、channel 的入口选择器，当前没有 oc run。命令和授权边界以 OMICSCLAW.md 及执行器帮助为准。