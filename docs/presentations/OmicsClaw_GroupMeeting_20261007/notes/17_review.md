完整回放之后，执行器生成 review_brief，分析者再写 REPORT，解释方法和结果。审查阶段给出 APPROVE 或 REVISE，最后由人确认是否 accept 并冻结模块。这几个状态分别表达运行、解释、检查和接受，不能彼此替代。

框架保存审查原文和 receipt，把审查绑定到对应 replay、步骤和报告哈希。这样修改了材料之后，就不能继续沿用旧审查当作当前版本的依据。receipt 是校验记录，没有数字签名；它提供一致性检查，不是对能同时改写工作区文件和回执的攻击者的防篡改保证。

接受后的模块需要修改时先 revise，保留已接受的 baseline，再重新执行与验收。冻结机制主要防止执行器无意覆盖已接受结果。相关逻辑见 omicsclaw/entry/review.py 和 skills/_sdk/notebook/。