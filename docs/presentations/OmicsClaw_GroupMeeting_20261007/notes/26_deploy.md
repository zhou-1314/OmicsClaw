部署有完整科学环境和轻量框架两条路径，项目要求 Python 三点十一或更高版本。conda 路径通过 0_setup_env.sh 准备 Python 科学栈、R 和外部 CLI，也包含 Desktop 后端依赖，适合在同一环境开展分析。

pip install -e . 可以先安装框架，使终端聊天和模型调用启动；它不等于装齐全部科学方法。Python 包的主要声明在 pyproject.toml，R、外部 CLI 和科学环境配置看 environment.yml。需要聊天平台 SDK 时还有对应的 channels 安装选项。

部署后通过 oc cli、oc desktop、oc channel 选择入口。这里最容易产生的误解，是把“能与模型对话”当成“全部算法都能运行”；真正执行前仍需核对可选后端、数据库和输入条件。安装依据是 README.md、pyproject.toml 和 environment.yml。