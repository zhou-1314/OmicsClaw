当前技能加载器识别八十八个技能，零个跳过；递归目录核对得到八十三个函数库和五个 CLI-only。函数库分布为单细胞二十七、空间十六、bulk RNA-seq 十四、基因组十、蛋白组八、代谢组七、文献一。对应交付过程保存在计划 0074 到 0076。

五个 CLI-only 分别是 sc-count、sc-fastq-qc、sc-velocity-prep、spatial-raw-processing 和 metabolomics-xcms-preprocessing。前三项与单细胞原始处理有关，空间原始处理也保留命令行边界，XCMS 则仅支持合成示例，明确拒绝真实 raw-MS 输入。

这些分类使调用方式更清楚，不能替代各后端的实际验收。真实 FASTQ 比对、原始质谱搜索、数据库和真实实验数据仍要按方法核验；读取已有结果的函数也不能被介绍成已经运行了完整上游工具链。