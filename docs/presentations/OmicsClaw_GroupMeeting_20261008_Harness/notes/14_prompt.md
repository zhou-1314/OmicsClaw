如果科学规范、环境信息和技能说明散落在调用模型的代码里，修改一条运行约定很难知道影响哪些请求。教程第十讲把 Prompt 当作可组装材料，OmicsClaw 由 entry 决定 Section 顺序，PromptAssembler 读取内容、跳过空段落、统计各段 token，再拼成一个 system 消息。

这些段落包括 OMICSCLAW.md 运行契约、安全与工具指导、计划、沙箱说明、技能目录、环境和长期记忆。教程用 AGENTS.md 演示工作区规则；本项目分析 Agent 的主要运行契约是 OMICSCLAW.md，不能把代码维护说明与分析运行契约混为一谈。

这种分解让输入可检查、可重新组织，但动态段落进入 system prompt 后，变化仍会影响前缀缓存。工具定义顺序稳定并不等于整段提示始终命中缓存。源码见 context/prompt.py、sections.py，以及 entry/assembly.py 的 default_sections。