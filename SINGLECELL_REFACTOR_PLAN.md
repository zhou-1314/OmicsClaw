# SingleCell Skills 重构方案
> 参考 Spatial Skills 架构，建立标准化的多方法选择机制

## 📋 重构目标

1. **统一方法注册机制**：每个技能都有 METHOD_REGISTRY + _METHOD_DISPATCH
2. **智能参数传递**：根据方法能力动态传递参数
3. **合理技能粒度**：保持与spatial相似的细分度
4. **完整测试覆盖**：为每个技能准备测试数据

---

## 🔧 技能规划（14个核心技能）

### 1️⃣ 基础流程（4个）

#### **sc-qc** - 质量控制指标计算
- **当前状态**：✅ 已实现，无方法选择
- **重构**：保持单一方法（scanpy标准QC）
- **测试数据**：pbmc3k_raw.h5ad

#### **sc-filter** - 细胞/基因过滤
- **当前状态**：✅ 已实现，支持组织特异性预设
- **重构**：
  ```python
  METHOD_REGISTRY = {
      "manual": MethodConfig("手动阈值过滤"),
      "mad": MethodConfig("MAD离群值检测"),
      "tissue_preset": MethodConfig("组织特异性预设"),
  }
  ```
- **测试数据**：pbmc3k_raw.h5ad

#### **sc-preprocessing** - 标准化预处理流程
- **当前状态**：✅ 已实现，单一scanpy流程
- **重构**：支持多种预处理策略
  ```python
  METHOD_REGISTRY = {
      "scanpy": MethodConfig(
          description="标准scanpy流程（normalize → log1p → HVG → scale → PCA → neighbors → UMAP → leiden）",
          dependencies=("scanpy",),
      ),
      "seurat": MethodConfig(
          description="Seurat风格（SCTransform归一化）",
          dependencies=("scanpy",),
      ),
      "pearson_residuals": MethodConfig(
          description="Pearson残差归一化（analytic Pearson residuals）",
          dependencies=("scanpy",),
      ),
  }
  ```
- **测试数据**：pbmc3k_raw.h5ad（过滤后）

#### **sc-doublet-detection** - Doublet检测
- **当前状态**：✅ 已实现，支持3种方法
- **重构**：已有METHOD_DISPATCH，需标准化为MethodConfig
  ```python
  METHOD_REGISTRY = {
      "scrublet": MethodConfig(
          description="Scrublet - 基于PCA的doublet检测",
          dependencies=("scrublet",),
      ),
      "scdblfinder": MethodConfig(
          description="scDblFinder - 基于模拟的doublet检测（R）",
          dependencies=("rpy2",),
          is_r_based=True,
      ),
      "doubletfinder": MethodConfig(
          description="DoubletFinder - 基于pANN的doublet检测（R）",
          dependencies=("rpy2",),
          is_r_based=True,
      ),
  }
  ```
- **测试数据**：pbmc3k_raw.h5ad（预处理后）

---

### 2️⃣ 下游分析（6个）

#### **sc-cell-annotation** - 细胞类型注释
- **当前状态**：✅ 已实现，支持4种方法
- **重构**：标准化方法注册
  ```python
  METHOD_REGISTRY = {
      "markers": MethodConfig(
          description="基于marker基因的手动注释",
          dependencies=("scanpy",),
      ),
      "celltypist": MethodConfig(
          description="CellTypist自动注释（深度学习）",
          dependencies=("celltypist",),
          supports_gpu=True,
      ),
      "singler": MethodConfig(
          description="SingleR参考数据集注释（R）",
          dependencies=("rpy2",),
          is_r_based=True,
      ),
      "scmap": MethodConfig(
          description="scmap投影注释",
          dependencies=("scanpy",),
      ),
  }
  ```
- **测试数据**：pbmc3k_processed.h5ad

#### **sc-de** - 差异表达分析
- **当前状态**：✅ 已实现，支持4种方法
- **重构**：
  ```python
  METHOD_REGISTRY = {
      "wilcoxon": MethodConfig(
          description="Wilcoxon秩和检验（非参数）",
          dependencies=("scanpy",),
      ),
      "t-test": MethodConfig(
          description="t检验（参数检验）",
          dependencies=("scanpy",),
      ),
      "mast": MethodConfig(
          description="MAST - 考虑dropout的混合模型",
          dependencies=("scanpy",),
      ),
      "deseq2": MethodConfig(
          description="PyDESeq2 pseudobulk分析",
          dependencies=("pydeseq2",),
      ),
  }
  ```
- **测试数据**：pbmc3k_processed.h5ad（需要分组信息）

#### **sc-markers** - Marker基因识别
- **当前状态**：✅ 已实现，支持3种方法
- **重构**：
  ```python
  METHOD_REGISTRY = {
      "wilcoxon": MethodConfig("Wilcoxon秩和检验"),
      "t-test": MethodConfig("t检验"),
      "logreg": MethodConfig("逻辑回归分类器"),
  }
  ```
- **测试数据**：pbmc3k_processed.h5ad

#### **sc-batch-integration** - 批次校正
- **当前状态**：✅ 已实现，支持6种方法，已有_dispatch_method
- **重构**：标准化为MethodConfig
  ```python
  METHOD_REGISTRY = {
      "harmony": MethodConfig(
          description="Harmony - 快速线性批次校正",
          dependencies=("harmonypy",),
      ),
      "scvi": MethodConfig(
          description="scVI - 变分自编码器（深度学习）",
          dependencies=("scvi-tools", "torch"),
          supports_gpu=True,
      ),
      "scanvi": MethodConfig(
          description="scANVI - 半监督scVI（需要部分标签）",
          dependencies=("scvi-tools", "torch"),
          supports_gpu=True,
      ),
      "bbknn": MethodConfig(
          description="BBKNN - 批次平衡k近邻",
          dependencies=("bbknn",),
      ),
      "fastmnn": MethodConfig(
          description="FastMNN - 互最近邻（R）",
          dependencies=("rpy2",),
          is_r_based=True,
      ),
      "scanorama": MethodConfig(
          description="Scanorama - 全景拼接",
          dependencies=("scanorama",),
      ),
  }
  ```
- **测试数据**：需要创建多batch数据（synthetic或下载）

#### **sc-pseudotime** - 伪时间分析
- **当前状态**：✅ 已实现，基于scanpy PAGA/DPT
- **重构**：
  ```python
  METHOD_REGISTRY = {
      "dpt": MethodConfig(
          description="Diffusion Pseudotime（扩散伪时间）",
          dependencies=("scanpy",),
      ),
      "paga": MethodConfig(
          description="PAGA - 分区图抽象",
          dependencies=("scanpy",),
      ),
      "palantir": MethodConfig(
          description="Palantir - 多分支轨迹",
          dependencies=("palantir",),
      ),
  }
  ```
- **测试数据**：pbmc3k_processed.h5ad

#### **sc-velocity** - RNA速度分析
- **当前状态**：✅ 已实现，基于scVelo
- **重构**：
  ```python
  METHOD_REGISTRY = {
      "scvelo_stochastic": MethodConfig(
          description="scVelo随机模型（快速）",
          dependencies=("scvelo",),
          requires_layers=("spliced", "unspliced"),
      ),
      "scvelo_dynamical": MethodConfig(
          description="scVelo动力学模型（精确）",
          dependencies=("scvelo",),
          requires_layers=("spliced", "unspliced"),
      ),
      "velovi": MethodConfig(
          description="VeloVI深度学习模型",
          dependencies=("scvi-tools", "torch"),
          supports_gpu=True,
          requires_layers=("spliced", "unspliced"),
      ),
  }
  ```
- **测试数据**：需要创建synthetic spliced/unspliced数据

---

### 3️⃣ 高级分析（4个）

#### **sc-grn** - 基因调控网络
- **当前状态**：✅ 已实现，基于pySCENIC
- **重构**：
  ```python
  METHOD_REGISTRY = {
      "pyscenic": MethodConfig(
          description="pySCENIC - GRNBoost2 + cisTarget + AUCell",
          dependencies=("pyscenic",),
      ),
      "celloracle": MethodConfig(
          description="CellOracle - 基于GRN的扰动预测",
          dependencies=("celloracle",),
      ),
  }
  ```
- **测试数据**：pbmc3k_processed.h5ad + 外部数据库

#### **sc-ambient-removal** - 环境RNA去除
- **当前状态**：✅ 已实现，支持3种方法
- **重构**：
  ```python
  METHOD_REGISTRY = {
      "cellbender": MethodConfig(
          description="CellBender - 深度学习去除环境RNA",
          dependencies=("cellbender",),
          supports_gpu=True,
      ),
      "soupx": MethodConfig(
          description="SoupX - 基于空液滴的校正（R）",
          dependencies=("rpy2",),
          is_r_based=True,
      ),
      "simple": MethodConfig(
          description="简单减法校正",
          dependencies=("scanpy",),
      ),
  }
  ```
- **测试数据**：pbmc3k_raw.h5ad（需要empty droplets信息）

#### **sc-cell-communication** - 细胞通讯
- **当前状态**：❌ 未实现（orchestrator中提到）
- **规划**：
  ```python
  METHOD_REGISTRY = {
      "liana": MethodConfig(
          description="LIANA+ - 多方法集成框架",
          dependencies=("liana",),
      ),
      "cellphonedb": MethodConfig(
          description="CellPhoneDB - 配体受体数据库",
          dependencies=("cellphonedb",),
      ),
      "nichenet": MethodConfig(
          description="NicheNet - 上下游调控预测（R）",
          dependencies=("rpy2",),
          is_r_based=True,
      ),
  }
  ```
- **测试数据**：pbmc3k_processed.h5ad（需要细胞类型标签）

#### **sc-multiome** - 多组学整合
- **当前状态**：❌ 未实现（orchestrator中提到）
- **规划**：
  ```python
  METHOD_REGISTRY = {
      "wnn": MethodConfig(
          description="WNN - 加权最近邻（Seurat风格）",
          dependencies=("muon",),
      ),
      "mofa": MethodConfig(
          description="MOFA+ - 多组学因子分析",
          dependencies=("mofapy2",),
      ),
      "totalvi": MethodConfig(
          description="totalVI - CITE-seq深度学习整合",
          dependencies=("scvi-tools", "torch"),
          supports_gpu=True,
      ),
  }
  ```
- **测试数据**：需要CITE-seq或multiome数据

---

## 🏗️ 通用框架设计

### MethodConfig 数据类
```python
@dataclass
class MethodConfig:
    name: str
    description: str
    dependencies: tuple[str, ...] = ()
    supports_gpu: bool = False
    is_r_based: bool = False
    requires_layers: tuple[str, ...] = ()  # 如 ('spliced', 'unspliced')
    requires_obs_keys: tuple[str, ...] = ()  # 如 ('batch',)
    min_cells: int = 100
    min_genes: int = 200
```

### 统一参数处理函数
```python
def build_method_kwargs(method: str, cfg: MethodConfig, args: argparse.Namespace) -> dict:
    """根据方法能力动态构建参数字典"""
    kwargs = {}

    # GPU参数
    if cfg.supports_gpu:
        kwargs["use_gpu"] = not getattr(args, 'no_gpu', False)

    # R方法参数
    if cfg.is_r_based:
        kwargs["r_seed"] = getattr(args, 'seed', 42)

    # 深度学习参数
    if "torch" in cfg.dependencies or "scvi" in cfg.dependencies:
        if hasattr(args, 'n_epochs') and args.n_epochs:
            kwargs["n_epochs"] = args.n_epochs
        if hasattr(args, 'learning_rate') and args.learning_rate:
            kwargs["learning_rate"] = args.learning_rate

    return kwargs
```

---

## 📊 测试数据准备

### 现有数据
1. ✅ **pbmc3k_raw.h5ad** (2700 cells × 32738 genes)
   - 用途：qc, filter, preprocessing, doublet, ambient-removal

2. ✅ **pbmc3k_processed.h5ad** (2638 cells × 1838 genes)
   - 用途：annotation, de, markers, pseudotime, grn
   - 特点：有8种细胞类型标签（louvain列）

### 需要创建的数据

3. **pbmc3k_multibatch.h5ad** - 多批次数据
   ```python
   # 从pbmc3k_processed创建，人工添加batch效应
   import scanpy as sc
   import numpy as np

   adata = sc.read_h5ad('data/pbmc3k_processed.h5ad')
   # 随机分成3个batch
   adata.obs['batch'] = np.random.choice(['batch1', 'batch2', 'batch3'], adata.n_obs)
   # 添加batch效应（简单版本）
   for i, batch in enumerate(['batch1', 'batch2', 'batch3']):
        mask = adata.obs['batch'] == batch
        adata.X[mask] *= (1.0 + i * 0.1)  # 简单的倍数效应
   adata.write_h5ad('data/pbmc3k_multibatch.h5ad')
   ```

4. **pbmc3k_velocity.h5ad** - 带spliced/unspliced的数据
   ```python
   # 创建synthetic velocity数据
   import scanpy as sc
   import numpy as np

   adata = sc.read_h5ad('data/pbmc3k_processed.h5ad')
   # 模拟spliced/unspliced counts
   adata.layers['spliced'] = adata.X.copy()
   adata.layers['unspliced'] = adata.X.copy() * np.random.uniform(0.3, 0.7, adata.shape)
   adata.write_h5ad('data/pbmc3k_velocity.h5ad')
   ```

---

## 🚀 实施步骤

### Phase 1: 框架建立（1-2天）
1. ✅ 创建 `omicsclaw/singlecell/method_config.py` - MethodConfig类
2. ✅ 创建 `omicsclaw/singlecell/method_utils.py` - 通用参数处理函数
3. ✅ 准备测试数据（multibatch, velocity）

### Phase 2: 核心技能重构（3-4天）
**优先级顺序**：
1. **sc-batch-integration** - 已有基础，快速标准化
2. **sc-velocity** - 你最近改进的，作为模板
3. **sc-preprocessing** - 基础技能，影响面大
4. **sc-cell-annotation** - 用户需求高

### Phase 3: 其他技能重构（2-3天）
5. sc-de, sc-markers, sc-doublet-detection
6. sc-pseudotime, sc-grn, sc-ambient-removal
7. sc-qc, sc-filter（保持简单）

### Phase 4: 新技能开发（可选，2-3天）
8. sc-cell-communication
9. sc-multiome

### Phase 5: 测试与文档（1-2天）
10. 为每个技能编写测试
11. 更新SKILL.md文档
12. 更新orchestrator路由表

---

## ✅ 验收标准

每个重构后的技能必须满足：
1. ✅ 有 METHOD_REGISTRY 和 _METHOD_DISPATCH
2. ✅ 支持 `--method` 参数选择方法
3. ✅ 智能参数传递（只传递方法支持的参数）
4. ✅ 有 `--demo` 模式（使用测试数据）
5. ✅ 生成标准化报告（markdown + JSON）
6. ✅ 通过至少1个方法的端到端测试

---

## 📝 示例：sc-velocity重构模板

```python
#!/usr/bin/env python3
"""Single-Cell RNA Velocity Analysis.

Supported methods:
  scvelo_stochastic   Fast stochastic model (default)
  scvelo_dynamical    Accurate dynamical model
  velovi              Deep learning model (GPU)

Usage:
    python sc_velocity.py --input <data.h5ad> --output <dir>
    python sc_velocity.py --input <data.h5ad> --method velovi --n-epochs 500 --output <dir>
    python sc_velocity.py --demo --output <dir>
"""

from dataclasses import dataclass

@dataclass
class MethodConfig:
    name: str
    description: str
    dependencies: tuple[str, ...] = ()
    supports_gpu: bool = False
    requires_layers: tuple[str, ...] = ()

METHOD_REGISTRY = {
    "scvelo_stochastic": MethodConfig(
        name="scvelo_stochastic",
        description="scVelo stochastic model (fast)",
        dependencies=("scvelo",),
        requires_layers=("spliced", "unspliced"),
    ),
    "scvelo_dynamical": MethodConfig(
        name="scvelo_dynamical",
        description="scVelo dynamical model (accurate)",
        dependencies=("scvelo",),
        requires_layers=("spliced", "unspliced"),
    ),
    "velovi": MethodConfig(
        name="velovi",
        description="VeloVI deep learning model",
        dependencies=("scvi", "torch"),
        supports_gpu=True,
        requires_layers=("spliced", "unspliced"),
    ),
}

def run_scvelo_stochastic(adata, **kwargs):
    import scvelo as scv
    scv.pp.filter_and_normalize(adata)
    scv.pp.moments(adata)
    scv.tl.velocity(adata, mode='stochastic')
    scv.tl.velocity_graph(adata)
    return {"method": "scvelo_stochastic", "n_cells": adata.n_obs}

def run_scvelo_dynamical(adata, **kwargs):
    import scvelo as scv
    scv.pp.filter_and_normalize(adata)
    scv.pp.moments(adata)
    scv.tl.recover_dynamics(adata)
    scv.tl.velocity(adata, mode='dynamical')
    scv.tl.velocity_graph(adata)
    scv.tl.latent_time(adata)
    return {"method": "scvelo_dynamical", "n_cells": adata.n_obs}

def run_velovi(adata, n_epochs=500, use_gpu=True, **kwargs):
    import scvi
    scvi.model.VELOVI.setup_anndata(adata, spliced_layer="spliced", unspliced_layer="unspliced")
    model = scvi.model.VELOVI(adata)
    model.train(max_epochs=n_epochs, use_gpu=use_gpu)
    latent_time = model.get_latent_time()
    velocities = model.get_velocity()
    adata.layers['velocity'] = velocities
    adata.obs['latent_time'] = latent_time
    return {"method": "velovi", "n_cells": adata.n_obs, "n_epochs": n_epochs}

_METHOD_DISPATCH = {
    "scvelo_stochastic": run_scvelo_stochastic,
    "scvelo_dynamical": run_scvelo_dynamical,
    "velovi": run_velovi,
}

def main():
    parser = argparse.ArgumentParser(description="Single-Cell RNA Velocity")
    parser.add_argument("--input", dest="input_path")
    parser.add_argument("--output", dest="output_dir", required=True)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--method", choices=list(METHOD_REGISTRY.keys()), default="scvelo_stochastic")
    parser.add_argument("--n-epochs", type=int, help="Number of epochs (for velovi)")
    parser.add_argument("--no-gpu", action="store_true", help="Disable GPU")
    args = parser.parse_args()

    # Load data
    if args.demo:
        adata = sc.read_h5ad('data/pbmc3k_velocity.h5ad')
    else:
        adata = sc.read_h5ad(args.input_path)

    # Check requirements
    cfg = METHOD_REGISTRY[args.method]
    for layer in cfg.requires_layers:
        if layer not in adata.layers:
            raise ValueError(f"Method '{args.method}' requires layer '{layer}'")

    # Build kwargs
    kwargs = {}
    if cfg.supports_gpu:
        kwargs["use_gpu"] = not args.no_gpu
    if args.n_epochs and "torch" in cfg.dependencies:
        kwargs["n_epochs"] = args.n_epochs

    # Run method
    run_fn = _METHOD_DISPATCH[args.method]
    summary = run_fn(adata, **kwargs)

    # Save results
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    adata.write_h5ad(output_dir / "processed.h5ad")

    # Generate report
    write_result_json(output_dir, "sc-velocity", "0.2.0", summary, {"method": args.method})

    print(f"Success: velocity analysis complete")
    print(f"  Method: {args.method}")
    print(f"  Output: {output_dir}")

if __name__ == "__main__":
    main()
```

---

## 🎯 总结

这个重构方案将：
1. ✅ 统一singlecell所有技能的方法选择机制
2. ✅ 与spatial保持一致的架构风格
3. ✅ 支持灵活的工具/算法选择
4. ✅ 提供完整的测试数据和测试覆盖
5. ✅ 保持代码简洁，避免过度工程化

预计总工时：**8-12天**（根据优先级可分阶段实施）
