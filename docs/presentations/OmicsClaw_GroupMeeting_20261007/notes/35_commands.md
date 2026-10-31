模块命令由 skills/_sdk/notebook/run.py 提供。new qc 创建模块、目录和策略模板；run analysis/01_qc 执行过期步骤；status 显示过期状态与原因；replay analysis/01_qc 从头执行模块，并把 validate 放在最后；accept 配合 review 文件，在用户确认后接受并冻结模块。

如果需要修改已经接受的模块，应当先 revise，保留旧 baseline。执行器需要在前台运行，以便取得完整结果与失败信息。页面为了便于定位展示仓库内路径；实际工作区调用时使用完整路径，或部署中已经配置好的 step runner 路径，并让输出留在分析工作区。

oc 是入口选择器，用来启动 cli、desktop 或 channel，当前没有 oc run。这里的命令已经通过实际 --help 核对，具体参数仍以当前执行器的帮助为准。