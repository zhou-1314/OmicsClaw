export const translations = {
  en: {
    // Navbar
    nav: {
      capabilities: "Capabilities",
      architecture: "Architecture",
      memory: "Memory",
      team: "Team",
      github: "GitHub",
    },

    // Hero
    hero: {
      badge: "v0.3.0 Available Now",
      title1: "Your Persistent AI Research Partner",
      title2: "for ",
      titleHighlight: "Multi-Omics Analysis",
      subtitle: "Remembers your data. Learns your preferences. Resumes your workflows. Conversational, memory-enabled, local-first multi-omics analysis platform.",
      cta1: "Explore Features",
      cta2: "Quick Start",
      cta3: "Documentation",
    },

    // Unified Control
    unified: {
      title1: "Unified Control, ",
      titleHighlight: "Different Surfaces",
      subtitle: "One powerful core agent seamlessly deployed across your terminal and messaging apps. Research anywhere.",
      cliTitle: "Interactive CLI / TUI",
      cliDesc: "Full-featured REPL environment for deep scientific exploration.",
      botTitle: "Mobile & Messaging Bots",
      chatUser: "Hey, check on the status of my scRNA-seq run.",
      chatBot: "Your pipeline finished. Memory saved 3 new datasets. Here is the trajectory plot.",
    },

    // Architecture
    arch: {
      title1: "Multi-Agent ",
      titleHighlight: "Architecture",
      subtitle: "Six specialized AI agents work in concert — from idea intake to peer-reviewed paper. Each agent has a clearly defined role, toolchain, and feedback loop.",
      loopLabel: "Reviewer → Writing feedback loop until acceptance",
      agents: [
        { tag: "01", title: "Planner Agent", desc: "Analyzes your paper + hypothesis and produces a staged experimental plan with success signals, QC thresholds, and skill dependencies.", tools: ["think_tool"] },
        { tag: "02", title: "Research Agent", desc: "Conducts web research for methods, baselines, datasets, and prior results. Returns actionable notes with cited sources.", tools: ["tavily_search", "think_tool"] },
        { tag: "03", title: "Coding Agent", desc: "Executes experiments in Jupyter notebooks. Searches OmicsClaw skill registry first, falls back to LLM-generated code if no skill exists.", tools: ["skill_search", "notebook_*", "think_tool"] },
        { tag: "04", title: "Analysis Agent", desc: "Interprets notebook outputs, computes metrics, creates publication-ready visualizations, and recommends next experiments.", tools: ["notebook_read", "think_tool"] },
        { tag: "05", title: "Writing Agent", desc: "Drafts a structured Markdown report with Abstract, Methods, Results, and Discussion. No fabricated results or citations.", tools: ["think_tool"] },
        { tag: "06", title: "Reviewer Agent", desc: "Peer-reviews the draft for logical consistency, reproducibility, and citation authenticity. Issues accept / revision / reject.", tools: ["tavily_search", "think_tool"] },
      ],
    },

    // Core Pillars
    pillars: {
      title1: "The 3 ",
      titleHighlight: "Core Pillars",
      p1Title: "1. Multi-Agent Research Pipeline",
      p1Desc: "From Scientific Idea to Full Research Paper. An orchestrated network of Planner, Researcher, Coder, and Reviewer agents completely hands-free.",
      p2Title: "2. Conversational Interface",
      p2Desc: "Chat with your Omics Data. Natural language UI abstracts away complex coding. Your constant online research assistant, ready to accept plain English commands and translate them into robust analytical pipelines.",
      p3Title: "3. Persistent Memory",
      p3Desc: "An AI Partner That Never Forgets. Graph-based memory tracks datasets, analysis history, user preferences, and biological insights across multiple sessions and tools. Stateless execution is a thing of the past.",
    },

    // Memory Showcase
    memory: {
      title1: "Interactive ",
      titleHighlight: "Memory Explorer",
      subtitle: "OmicsClaw extends multi-omics analysis beyond stateless execution toward an interactive memory-enabled paradigm. Audit analyses, track datasets, and manage research preferences visually.",
      stats: [
        { label: "Datasets", count: "Auto-Tracked" },
        { label: "Analyses", count: "Full Lineage" },
        { label: "Preferences", count: "User Params" },
        { label: "Insights", count: "Bio Nodes" },
      ],
    },

    // Domains
    domains: {
      title1: "Multi-Omics ",
      titleHighlight: "Coverage",
      subtitle: "6 Domains. 63+ Standardized Skills. Fully reproducible execution.",
      list: [
        { name: "Spatial Transcriptomics", count: 15, highlights: "QC, Domains, Deconvolution, Statistics" },
        { name: "Single-Cell Omics", count: 13, highlights: "Annotation, Velocity, Batch Integration" },
        { name: "Genomics", count: 10, highlights: "Variant Calling, CNV, Assembly, Phasing" },
        { name: "Proteomics", count: 8, highlights: "MS QC, Identification, Quantification" },
        { name: "Metabolomics", count: 8, highlights: "Peak Detection, XCMS, Enrichment" },
        { name: "Bulk RNA-seq", count: 13, highlights: "DE, Trajectory Interpolation, Survival" },
      ],
      skillsUnit: "Native Skills",
      keyCapabilities: "Key Capabilities:",
    },

    // Team
    team: {
      title1: "The ",
      titleHighlight: "Team",
      subtitle: "Researchers and developers dedicated to transforming multi-omics analysis.",
    },

    // Footer
    footer: {
      desc: "Your Persistent AI Research Partner for Multi-Omics Analysis. Conversational, Memory-enabled, and Local-first.",
      resources: "Resources",
      ghRepo: "GitHub Repository",
      install: "Installation Guide",
      agents: "Agents Reference",
      legal: "Legal",
      license: "Apache-2.0 License",
      research: "Research Use Only",
      copyright: "© 2026 OmicsClaw. Built for the scientific community.",
    },
  },

  zh: {
    // Navbar
    nav: {
      capabilities: "核心能力",
      architecture: "系统架构",
      memory: "记忆系统",
      team: "团队",
      github: "GitHub",
    },

    // Hero
    hero: {
      badge: "v0.3.0 现已发布",
      title1: "你的持久化 AI 科研伙伴",
      title2: "专注于",
      titleHighlight: "多组学分析",
      subtitle: "记住你的数据、学习你的偏好、恢复你的工作流。对话式、记忆驱动、本地优先的多组学分析平台。",
      cta1: "探索功能",
      cta2: "快速上手",
      cta3: "阅读文档",
    },

    // Unified Control
    unified: {
      title1: "统一控制，",
      titleHighlight: "多端触达",
      subtitle: "一个强大的核心 Agent，无缝部署在你的终端和消息应用中。随时随地开展研究。",
      cliTitle: "交互式 CLI / TUI 终端",
      cliDesc: "功能完备的 REPL 环境，适用于深度科学探索。",
      botTitle: "移动端 & 即时通讯机器人",
      chatUser: "帮我看下 scRNA-seq 运行状态。",
      chatBot: "你的流水线已完成。记忆系统保存了 3 个新数据集。这是轨迹分析图。",
    },

    // Architecture
    arch: {
      title1: "多智能体",
      titleHighlight: "协作架构",
      subtitle: "六个专业 AI 智能体协同工作 —— 从想法接收到同行评审论文。每个智能体都有明确的角色定义、工具链和反馈回路。",
      loopLabel: "Reviewer → Writing 反馈循环，直至通过评审",
      agents: [
        { tag: "01", title: "Planner 规划智能体", desc: "分析论文和用户假设，生成分阶段实验计划，包含成功指标、QC 阈值和技能依赖。", tools: ["think_tool"] },
        { tag: "02", title: "Research 检索智能体", desc: "在线检索方法论、基线数据集和前人结果，返回带引用来源的可执行笔记。", tools: ["tavily_search", "think_tool"] },
        { tag: "03", title: "Coding 编码智能体", desc: "在 Jupyter Notebook 中执行实验。优先搜索 OmicsClaw 技能注册表，无匹配时回退到 LLM 自动生成代码。", tools: ["skill_search", "notebook_*", "think_tool"] },
        { tag: "04", title: "Analysis 分析智能体", desc: "解读 Notebook 输出，计算指标，生成发表级可视化图表，并推荐后续实验方向。", tools: ["notebook_read", "think_tool"] },
        { tag: "05", title: "Writing 写作智能体", desc: "撰写结构化 Markdown 报告，包含摘要、方法、结果和讨论。不允许捏造数据或引用。", tools: ["think_tool"] },
        { tag: "06", title: "Reviewer 评审智能体", desc: "对草稿进行同行评审，检查逻辑一致性、可复现性和引文真实性。给出 accept / revision / reject。", tools: ["tavily_search", "think_tool"] },
      ],
    },

    // Core Pillars
    pillars: {
      title1: "三大",
      titleHighlight: "核心支柱",
      p1Title: "1. 多智能体研究流水线",
      p1Desc: "从科学想法到完整研究论文。由 Planner、Researcher、Coder、Reviewer 组成的智能体网络全自动编排，彻底解放双手。",
      p2Title: "2. 对话式交互界面",
      p2Desc: "用自然语言与你的组学数据对话。无需手写复杂的 Python/R 脚本。你的常驻在线研究助手，用简单的中英文指令驱动强大的分析管线。",
      p3Title: "3. 持久化记忆系统",
      p3Desc: "一个永不遗忘的 AI 伙伴。图神经网络记忆系统跨会话追踪数据集、分析历史、用户偏好和生物学发现。无状态执行已成过去式。",
    },

    // Memory Showcase
    memory: {
      title1: "交互式",
      titleHighlight: "记忆探索器",
      subtitle: "OmicsClaw 超越了无状态执行，迈向了交互式、记忆驱动的全新范式。可视化审计分析历史、追踪数据集、管理研究偏好。",
      stats: [
        { label: "数据集", count: "自动追踪" },
        { label: "分析记录", count: "全链路溯源" },
        { label: "偏好设置", count: "用户参数" },
        { label: "科学发现", count: "生物节点" },
      ],
    },

    // Domains
    domains: {
      title1: "多组学",
      titleHighlight: "领域覆盖",
      subtitle: "6 大领域，63+ 标准化技能，完全可复现的执行环境。",
      list: [
        { name: "空间转录组学", count: 15, highlights: "质控、空间域识别、反卷积、统计分析" },
        { name: "单细胞组学", count: 13, highlights: "注释、RNA 速率、批次整合" },
        { name: "基因组学", count: 10, highlights: "变异检测、CNV、组装、分相" },
        { name: "蛋白质组学", count: 8, highlights: "质谱质控、鉴定、定量" },
        { name: "代谢组学", count: 8, highlights: "峰检测、XCMS、富集分析" },
        { name: "Bulk RNA-seq", count: 13, highlights: "差异表达、轨迹插值、生存分析" },
      ],
      skillsUnit: "个原生技能",
      keyCapabilities: "核心能力：",
    },

    // Team
    team: {
      title1: "",
      titleHighlight: "团队",
      subtitle: "致力于推动多组学分析变革的研究者与开发者。",
    },

    // Footer
    footer: {
      desc: "你的持久化 AI 科研伙伴，专注于多组学分析。对话式、记忆驱动、本地优先。",
      resources: "资源链接",
      ghRepo: "GitHub 仓库",
      install: "安装指南",
      agents: "智能体参考",
      legal: "法律信息",
      license: "Apache-2.0 许可证",
      research: "仅供科研使用",
      copyright: "© 2026 OmicsClaw. 为科学社区而建。",
    },
  },
};
