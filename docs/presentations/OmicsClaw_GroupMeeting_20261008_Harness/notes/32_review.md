当前交互把验证结果、独立审查和接受分开。分析完成 replay 并写出 REPORT 后，应立即展示结果；独立审查默认关闭。Desktop 的 Independent review 按钮开启一个新回合，只为这一回合授权审查，后续普通回合恢复关闭。CLI 和 Channel 也只在当前用户明确请求时审查。

用户请求审查后，reviewer 检查对应模块材料，报告 findings。框架保存原始回复与 receipt，绑定 replay 和文件哈希；材料变化后不能沿用旧审查。这个请求不授权自动修改问题、重跑分析或接受模块。receipt 只是校验记录，没有数字签名。

accept 必须由用户明确决定。默认不审查不等于已经批准；用户若明确接受且明确跳过审查，才使用 --skip-review，图中虚线表达这条路径。修改已接受模块先 revise，保留 baseline。依据是当前 OMICSCLAW.md 与 entry/review.py。