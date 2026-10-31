假设每增加一个组学方法就增加一个模型工具定义，方法库扩大时，每轮要携带的名称、参数和说明也会越来越多。教程第六讲从这个问题讨论少量通用原语，OmicsClaw 的科学方法执行也利用了这种组合方式。

read_file 读取说明与结果，write_file 创建分析步骤，edit_file 修改其中一部分，bash 真正运行步骤。模型先用 use_skill 取得方法说明，再写出 step；step 用 load_skill 调函数库，用 run_cli 调 CLI-only 方法。因此新增方法主要增加文档与科学实现，不必给 engine 增加一个算法分支。

项目并不只有这四个工具，还按职责提供技能、计划、记忆、web 和 MCP 等能力。教程对 YOLO 的讨论也不能解释成本项目默认免审批，通用 bash 的副作用仍由权限和执行环境限制。源码见 tools/builtin/，调用约定见 OMICSCLAW.md。