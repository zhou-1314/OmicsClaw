# SingleCell Skills重构计划

## 当前状况
- sc-batch-integration已有基础方法选择：`_dispatch_method`
- sc-velocity、sc-preprocessing等技能缺乏方法选择
- 缺乏统一的参数管理和依赖检查

## 重构目标
参考spatial skills的成功模式，为singlecell建立：
1. 统一的方法注册机制
2. 智能参数传递系统
3. 依赖管理和GPU检测
4. 标准化的错误处理

## 具体重构步骤

### 第一步：重构sc-preprocessing
当前只有scanpy流程，应该支持多种工具：

```python
METHOD_REGISTRY = {
    "scanpy": MethodConfig(
        description="Standard scanpy preprocessing pipeline",
        dependencies=("scanpy",),
        supports_gpu=False,
    ),
    "seurat": MethodConfig(
        description="Seurat-style preprocessing via scanpy",
        dependencies=("scanpy",),
        supports_gpu=False,
    ),
    "pegasus": MethodConfig(
        description="Fast preprocessing with pegasus",
        dependencies=("pegasus",),
        supports_gpu=True,
    ),
    "rapids": MethodConfig(
        description="GPU-accelerated with cuML/rapids",
        dependencies=("cuml", "scanpy"),
        supports_gpu=True,
    ),
}
```

### 第二步：扩展sc-velocity
添加多种velocity方法：

```python
METHOD_REGISTRY = {
    "scvelo_stochastic": MethodConfig(
        description="scVelo stochastic model (fast)",
        dependencies=("scvelo",),
        supports_gpu=False,
    ),
    "scvelo_dynamical": MethodConfig(
        description="scVelo dynamical model (accurate)",
        dependencies=("scvelo",),
        supports_gpu=False,
    ),
    "velovi": MethodConfig(
        description="VeloVI deep learning model",
        dependencies=("scvi", "torch"),
        supports_gpu=True,
    ),
    "cellrank": MethodConfig(
        description="CellRank-based velocity analysis",
        dependencies=("cellrank", "scvelo"),
        supports_gpu=False,
    ),
}
```

### 第三步：增强sc-cell-annotation
支持多种注释方法：

```python
METHOD_REGISTRY = {
    "celltypist": MethodConfig(
        description="CellTypist automated annotation",
        dependencies=("celltypist",),
        supports_gpu=True,
    ),
    "singler": MethodConfig(
        description="SingleR reference-based (via rpy2)",
        dependencies=("rpy2",),
        is_r_based=True,
    ),
    "scmap": MethodConfig(
        description="scmap projection method",
        dependencies=("scanpy",),
        supports_gpu=False,
    ),
    "garnet": MethodConfig(
        description="GARNET network-based annotation",
        dependencies=("garnet",),
        supports_gpu=True,
    ),
}
```

### 第四步：新增sc-trajectory
分离伪时间分析：

```python
METHOD_REGISTRY = {
    "dpt": MethodConfig(
        description="Diffusion pseudotime (scanpy)",
        dependencies=("scanpy",),
        supports_gpu=False,
    ),
    "paga": MethodConfig(
        description="PAGA trajectory inference",
        dependencies=("scanpy",),
        supports_gpu=False,
    ),
    "cellrank": MethodConfig(
        description="CellRank fate mapping",
        dependencies=("cellrank",),
        supports_gpu=False,
    ),
    "palantir": MethodConfig(
        description="Palantir multifurcating trajectories",
        dependencies=("palantir",),
        supports_gpu=False,
    ),
    "slingshot": MethodConfig(
        description="Slingshot (via rpy2)",
        dependencies=("rpy2",),
        is_r_based=True,
    ),
}
```

## 通用框架设计

### 方法配置类
```python
@dataclass
class MethodConfig:
    name: str
    description: str
    dependencies: tuple[str, ...] = ()
    supports_gpu: bool = False
    is_r_based: bool = False
    requires_layers: tuple[str, ...] = ()  # 如 ('spliced', 'unspliced')
    min_cells: int = 100
    min_genes: int = 200
```

### 统一参数处理
```python
def build_method_kwargs(method: str, args: argparse.Namespace) -> dict:
    """根据方法能力动态构建参数字典"""
    cfg = METHOD_REGISTRY[method]
    kwargs = {"method": method}

    # GPU参数
    if cfg.supports_gpu:
        kwargs["use_gpu"] = not getattr(args, 'no_gpu', False)

    # R方法参数
    if cfg.is_r_based:
        kwargs["r_seed"] = args.seed

    # 深度学习参数
    if "torch" in cfg.dependencies:
        if args.n_epochs:
            kwargs["n_epochs"] = args.n_epochs
        if args.learning_rate:
            kwargs["learning_rate"] = args.learning_rate

    return kwargs
```

## 重构优先级
1. **高优先级**：sc-preprocessing, sc-velocity（与你最近的GRN/velocity工作相关）
2. **中优先级**：sc-cell-annotation, sc-trajectory
3. **低优先级**：sc-de, sc-markers（功能相对简单）

## 实施建议
1. 先选择一个技能（建议sc-velocity）作为模板
2. 实现完整的方法注册和分发机制
3. 测试确认无误后，复制到其他技能
4. 逐步增加新方法支持