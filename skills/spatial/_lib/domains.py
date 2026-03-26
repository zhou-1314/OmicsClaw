"""Spatial domain identification algorithms.

Provides multiple methods for tissue region/niche identification:
  - leiden:   Graph-based clustering with spatial-weighted neighbors (default)
  - louvain:  Classic graph-based clustering
  - spagcn:   Spatial Graph Convolutional Network
  - stagate:  Graph attention auto-encoder (PyTorch Geometric)
  - graphst:  Self-supervised contrastive learning (PyTorch)
  - banksy:   Explicit spatial feature augmentation

Usage::

    from skills.spatial._lib.domains import (
        identify_domains_leiden,
        identify_domains_spagcn,
        refine_spatial_domains,
        SUPPORTED_METHODS,
    )

    summary = identify_domains_leiden(adata, resolution=1.0)
"""

from __future__ import annotations

import logging
from collections import Counter

import numpy as np
import pandas as pd
import scanpy as sc

from .adata_utils import ensure_neighbors, ensure_pca, get_spatial_key, require_spatial_coords

logger = logging.getLogger(__name__)

SUPPORTED_METHODS = ("leiden", "louvain", "spagcn", "stagate", "graphst", "banksy")


# ---------------------------------------------------------------------------
# Spatial domain refinement (shared across methods)
# ---------------------------------------------------------------------------


def refine_spatial_domains(
    adata,
    domain_key: str = "spatial_domain",
    *,
    threshold: float = 0.5,
    k: int = 10,
) -> pd.Series:
    """Spatially smooth domain labels using k-nearest neighbor majority vote.

    Only relabels a spot when >threshold fraction of its neighbors disagree,
    following the approach from Hu et al., Nature Methods 2021 (SpaGCN).
    """
    from sklearn.neighbors import NearestNeighbors

    spatial_key = get_spatial_key(adata)
    if spatial_key is None:
        return adata.obs[domain_key]

    coords = adata.obsm[spatial_key]
    labels = adata.obs[domain_key].values.astype(str)

    k = min(k, len(labels) - 1)
    if k < 1:
        return pd.Series(labels, index=adata.obs.index)

    nbrs = NearestNeighbors(n_neighbors=k).fit(coords)
    _, indices = nbrs.kneighbors(coords)

    refined = []
    for i, neighbors in enumerate(indices):
        neighbor_labels = labels[neighbors]
        different_ratio = np.sum(neighbor_labels != labels[i]) / len(neighbor_labels)
        if different_ratio >= threshold:
            most_common = Counter(neighbor_labels).most_common(1)[0][0]
            refined.append(most_common)
        else:
            refined.append(labels[i])

    return pd.Series(refined, index=adata.obs.index)


# ---------------------------------------------------------------------------
# Domain identification methods
# ---------------------------------------------------------------------------


def identify_domains_leiden(
    adata,
    *,
    resolution: float = 1.0,
    n_neighbors: int = 15,
    n_pcs: int = 50,
    spatial_weight: float = 0.3,
) -> dict:
    """Leiden clustering on a composite expression + spatial graph.

    Leiden operates on the **pre-built neighbor graph** derived from
    log-normalized expression + PCA. When spatial coordinates are
    available, the expression-based and spatial-based neighbor graphs are
    combined with configurable weighting.
    """
    logger.info(
        "Leiden: using pre-built neighbor graph (log-normalized + PCA); "
        "spatial_weight=%.2f", spatial_weight,
    )
    ensure_pca(adata, n_comps=n_pcs)
    ensure_neighbors(adata, n_neighbors=n_neighbors, n_pcs=min(n_pcs, 30))

    spatial_key = get_spatial_key(adata)
    adjacency = adata.obsp["connectivities"]

    if spatial_key is not None and spatial_weight > 0:
        try:
            import squidpy as sq
            sq.gr.spatial_neighbors(adata, spatial_key=spatial_key, coord_type="generic")
            if "spatial_connectivities" in adata.obsp:
                expr_w = 1.0 - spatial_weight
                # Mathematical combination without permanently overwriting adata.obsp['connectivities']
                adjacency = (
                    expr_w * adata.obsp["connectivities"]
                    + spatial_weight * adata.obsp["spatial_connectivities"]
                )
                logger.info(
                    "Dynamically integrated expression (%.0f%%) + spatial (%.0f%%) graphs",
                    expr_w * 100, spatial_weight * 100,
                )
        except Exception as e:
            logger.warning("Could not build spatial graph, using expression only: %s", e)

    sc.tl.leiden(adata, resolution=resolution, flavor="igraph", key_added="spatial_domain", adjacency=adjacency)

    n_domains = adata.obs["spatial_domain"].nunique()
    logger.info("Leiden domains: %d (resolution=%.2f)", n_domains, resolution)

    return {
        "method": "leiden",
        "n_domains": n_domains,
        "resolution": resolution,
        "spatial_weight": spatial_weight if spatial_key else 0.0,
        "domain_counts": adata.obs["spatial_domain"].value_counts().to_dict(),
    }


def identify_domains_louvain(
    adata,
    *,
    resolution: float = 1.0,
    n_neighbors: int = 15,
    n_pcs: int = 50,
    spatial_weight: float = 0.0,
) -> dict:
    """Louvain graph clustering for spatial domain identification.

    Like Leiden, Louvain operates on the neighbor graph (derived from 
    log-normalized + PCA). If `spatial_weight > 0.0`, a composite graph is built.

    Requires the ``louvain`` Python package: pip install louvain
    """
    logger.info(
        "Louvain: using pre-built neighbor graph (log-normalized + PCA); "
        "spatial_weight=%.2f", spatial_weight,
    )
    ensure_pca(adata, n_comps=n_pcs)
    ensure_neighbors(adata, n_neighbors=n_neighbors, n_pcs=min(n_pcs, 30))

    try:
        import louvain as _  # noqa: F401
    except ImportError:
        raise ImportError(
            "'louvain' is not installed.\n\n"
            "Install:     pip install louvain\n"
            "Alternative: use --method leiden (bundled with scanpy/leidenalg)"
        )

    spatial_key = get_spatial_key(adata)
    adjacency = adata.obsp["connectivities"]

    if spatial_key is not None and spatial_weight > 0:
        try:
            import squidpy as sq
            sq.gr.spatial_neighbors(adata, spatial_key=spatial_key, coord_type="generic")
            if "spatial_connectivities" in adata.obsp:
                expr_w = 1.0 - spatial_weight
                adjacency = (
                    expr_w * adata.obsp["connectivities"]
                    + spatial_weight * adata.obsp["spatial_connectivities"]
                )
                logger.info(
                    "Dynamically integrated expression (%.0f%%) + spatial (%.0f%%) graphs",
                    expr_w * 100, spatial_weight * 100,
                )
        except Exception as e:
            logger.warning("Could not build spatial graph, using expression only: %s", e)

    sc.tl.louvain(adata, resolution=resolution, key_added="spatial_domain", adjacency=adjacency)

    n_domains = adata.obs["spatial_domain"].nunique()
    logger.info("Louvain domains: %d (resolution=%.2f)", n_domains, resolution)

    return {
        "method": "louvain",
        "n_domains": n_domains,
        "resolution": resolution,
        "spatial_weight": spatial_weight if spatial_key else 0.0,
        "domain_counts": adata.obs["spatial_domain"].value_counts().to_dict(),
    }


def identify_domains_spagcn(
    adata,
    *,
    n_domains: int = 7,
) -> dict:
    """SpaGCN — Spatial Graph Convolutional Network for domain identification.

    Uses **log-normalized expression** (``adata.X``) plus spatial
    coordinates. SpaGCN performs its own internal PCA on the expression
    matrix. Optionally integrates histology images.
    """
    from .dependency_manager import require

    require("SpaGCN", feature="SpaGCN spatial domain detection")

    import scipy.sparse
    import SpaGCN

    # SpaGCN 1.2.7 uses .A (removed in scipy >= 1.14); patch for compatibility across formats
    for mat_type in (scipy.sparse.csr_matrix, scipy.sparse.csc_matrix, scipy.sparse.coo_matrix):
        if not hasattr(mat_type, "A"):
            mat_type.A = property(lambda self: self.toarray())

    from .adata_utils import get_spatial_key
    spatial_key = get_spatial_key(adata)
    if spatial_key is None:
        raise ValueError("SpaGCN requires spatial coordinates, but none were found in adata.obsm.")
    
    coords = adata.obsm[spatial_key]

    logger.info(
        "SpaGCN: using log-normalized expression (adata.X, %d genes) "
        "+ spatial coordinates", adata.n_vars,
    )

    x_coord = coords[:, 0].astype(float)
    y_coord = coords[:, 1].astype(float)

    logger.info("Building SpaGCN adjacency matrix ...")
    adj = SpaGCN.calculate_adj_matrix(x=x_coord, y=y_coord, histology=False)

    l_value = SpaGCN.search_l(0.5, adj, start=0.01, end=1000, tol=0.01, max_run=100)
    logger.info("SpaGCN optimized l-parameter: %.4f", l_value)

    # Auto-detect topology shape for spatial refinement
    shape = "square"  # Default for ST, Stereo-seq, etc.
    if "spatial" in adata.uns:
        # Detect 10X Visium
        for k, v in adata.uns["spatial"].items():
            if "visium" in str(k).lower() or "visium" in str(v).lower():
                shape = "hexagon"
                break
    logger.info("SpaGCN refine topology shape mode: %s", shape)

    import gc
    import torch
    
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        logger.info("SpaGCN using CUDA accelerator")

    clf = SpaGCN.SpaGCN()
    clf.set_l(l_value)
    clf.train(
        adata, adj,
        num_pcs=50, init_spa=True, init="louvain",
        res=0.4, tol=5e-3, lr=0.05, max_epochs=200,
        n_clusters=n_domains,
    )

    y_pred, prob = clf.predict()
    adata.obs["spatial_domain"] = pd.Categorical(y_pred.astype(str))

    logger.info("Running spatial domain boundary refinement...")
    try:
        refined = SpaGCN.refine(
            sample_id=adata.obs.index.tolist(),
            pred=y_pred, dis=adj, shape=shape,
        )
        adata.obs["spatial_domain"] = pd.Categorical([str(r) for r in refined])
    except Exception as e:
        logger.warning(
            "SpaGCN refinement failed or is mathematically incompatible with the coordinate density (shape=%s). "
            "Falling back to unrefined predictions. Error: %s", shape, e
        )

    # Memory cleanup
    del clf
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    actual_n = adata.obs["spatial_domain"].nunique()
    logger.info("SpaGCN domains: %d (requested %d)", actual_n, n_domains)

    return {
        "method": "spagcn",
        "n_domains": actual_n,
        "n_domains_requested": n_domains,
        "topology_shape": shape,
        "domain_counts": adata.obs["spatial_domain"].value_counts().to_dict(),
    }


def identify_domains_stagate(
    adata,
    *,
    n_domains: int = 7,
    rad_cutoff: float | None = None,
    k_nn: int | None = 6,
    random_seed: int = 42,
) -> dict:
    """STAGATE — graph attention auto-encoder for spatial domain identification.

    Uses **log-normalized expression** (``adata.X``), automatically subsetted
    to highly variable genes when available.
    Spatial network is built preferably via scale-invariant KNN (`k_nn=6`)
    rather than a fixed radius to support diverse spatial resolutions.
    """
    from .dependency_manager import require

    require("STAGATE_pyG", feature="STAGATE spatial domain identification")
    require("torch", feature="STAGATE (PyTorch backend)")

    import torch
    import STAGATE_pyG
    import gc

    logger.info("Running STAGATE (k_nn=%s, rad_cutoff=%s, n_domains=%d) ...", k_nn, rad_cutoff, n_domains)

    if "highly_variable" in adata.var.columns and adata.var["highly_variable"].sum() > 0:
        n_hvg = adata.var["highly_variable"].sum()
        logger.info("Subsetting to %d HVGs for STAGATE autoencoder", n_hvg)
        adata_work = adata[:, adata.var["highly_variable"]].copy()
    else:
        logger.warning(
            "No valid 'highly_variable' mask found; using all %d genes. "
            "Consider running sc.pp.highly_variable_genes() first to prevent VRAM explosion.",
            adata.n_vars,
        )
        adata_work = adata.copy()

    # Build Spatial Network
    # KNN is strongly preferred as it is invariant to coordinate scaling (Stereo-seq vs Visium)
    if k_nn is not None and k_nn > 0:
        logger.info("Building STAGATE network using adaptive KNN (k=%d)", k_nn)
        STAGATE_pyG.Cal_Spatial_Net(adata_work, model='KNN', k_cutoff=k_nn)
    elif rad_cutoff is not None and rad_cutoff > 0:
        logger.warning("Building STAGATE network using static radius (rad=%.1f). May fail if coordinate scales misalign.", rad_cutoff)
        STAGATE_pyG.Cal_Spatial_Net(adata_work, model='Radius', rad_cutoff=rad_cutoff)
    else:
        logger.warning("No spatial geometry passed. Falling back to scale-invariant KNN (k=6).")
        STAGATE_pyG.Cal_Spatial_Net(adata_work, model='KNN', k_cutoff=6)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    logger.info("STAGATE device: %s", device)
    
    if device.type == "cuda":
        torch.cuda.empty_cache()

    try:
        adata_work = STAGATE_pyG.train_STAGATE(adata_work, device=device)
    finally:
        # Mandatory cleanup to prevent CUDA OOM across multiple runs
        if device.type == "cuda":
            torch.cuda.empty_cache()
        gc.collect()

    from sklearn.mixture import GaussianMixture
    from sklearn.cluster import KMeans

    embedding = adata_work.obsm["STAGATE"]
    try:
        gmm = GaussianMixture(
            n_components=n_domains, covariance_type="tied",
            random_state=random_seed, reg_covar=1e-3,  # Increased reg_covar for conditioning
        )
        labels = gmm.fit_predict(embedding)
        clustering_name = "gmm_tied"
    except Exception as e:
        logger.warning("GMM failed (%s), falling back to KMeans", e)
        kmeans = KMeans(n_clusters=n_domains, random_state=random_seed, n_init=10)
        labels = kmeans.fit_predict(embedding)
        clustering_name = "kmeans"

    adata.obs["spatial_domain"] = pd.Categorical(labels.astype(str))
    adata.obsm["X_stagate"] = embedding

    actual_n = adata.obs["spatial_domain"].nunique()
    logger.info("STAGATE domains: %d (requested %d)", actual_n, n_domains)

    return {
        "method": "stagate",
        "n_domains": actual_n,
        "n_domains_requested": n_domains,
        "k_nn": k_nn,
        "rad_cutoff": rad_cutoff,
        "clustering": clustering_name,
        "device": str(device),
        "domain_counts": adata.obs["spatial_domain"].value_counts().to_dict(),
    }


def identify_domains_graphst(
    adata,
    *,
    n_domains: int = 7,
    random_seed: int = 42,
) -> dict:
    """GraphST — self-supervised contrastive learning for spatial domains.

    Important: GraphST.preprocess() internally performs log1p + normalize +
    scale + HVG selection. In order to avoid double log-transforms, raw
    counts are automatically restored from `.layers['counts']` or `.raw`.
    """
    from .dependency_manager import require

    require("GraphST", feature="GraphST spatial domain identification")
    require("torch", feature="GraphST (PyTorch backend)")

    import torch
    import gc
    from GraphST import GraphST as GraphSTModule

    logger.info("Running GraphST (n_domains=%d) ...", n_domains)

    # Priority 1: adata.layers["counts"]
    # Priority 2: adata.raw
    # Priority 3: Fallback to adata.X (with warning)
    if "counts" in adata.layers:
        logger.info("Restoring raw counts from adata.layers['counts'] for GraphST preprocessing")
        adata_work = adata.copy()
        adata_work.X = adata_work.layers["counts"].copy()
    elif adata.raw is not None:
        logger.info(
            "Restoring raw counts from adata.raw for GraphST "
            "(avoids double log-transform)"
        )
        adata_work = adata.raw.to_adata().copy()
        spatial_key = get_spatial_key(adata)
        if spatial_key and spatial_key in adata.obsm:
            adata_work.obsm[spatial_key] = adata.obsm[spatial_key]
    else:
        logger.warning(
            "Neither adata.layers['counts'] nor adata.raw found — using adata.X directly. "
            "If adata.X is already log-normalized, GraphST results may be suboptimal."
        )
        adata_work = adata.copy()

    # GraphST strictly expects geometry in "spatial"
    spatial_key = get_spatial_key(adata_work)
    if spatial_key and spatial_key != "spatial":
        adata_work.obsm["spatial"] = adata_work.obsm[spatial_key]

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if device.type == "cuda":
        torch.cuda.empty_cache()
        logger.info("GraphST using CUDA accelerator")

    # Feature Preparation Pipeline
    GraphSTModule.preprocess(adata_work)
    GraphSTModule.construct_interaction(adata_work)

    from GraphST.GraphST import GraphST as GraphSTModel

    try:
        model = GraphSTModel(adata_work, device=device, random_seed=random_seed)
        adata_work = model.train()
    finally:
        # Prevent PyTorch OOM leaking graph embeddings
        if device.type == "cuda":
            torch.cuda.empty_cache()
        gc.collect()

    from sklearn.decomposition import PCA
    from sklearn.mixture import GaussianMixture
    from sklearn.cluster import KMeans

    # GMM clustering generally requires dimensionality reduction first to prevent ill-conditioned covariance grids
    pca = PCA(n_components=15, random_state=random_seed)
    embedding = pca.fit_transform(adata_work.obsm["emb"])

    try:
        gmm = GaussianMixture(
            n_components=n_domains, covariance_type="tied",
            random_state=random_seed, reg_covar=1e-3, # Increased precision buffer
        )
        labels = gmm.fit_predict(embedding)
        clustering_name = "gmm_tied"
    except Exception as e:
        logger.warning("GMM failed (%s), falling back to KMeans", e)
        kmeans = KMeans(n_clusters=n_domains, random_state=random_seed, n_init=10)
        labels = kmeans.fit_predict(embedding)
        clustering_name = "kmeans"

    adata.obs["spatial_domain"] = pd.Categorical(labels.astype(str))
    adata.obsm["X_graphst"] = adata_work.obsm["emb"]

    actual_n = adata.obs["spatial_domain"].nunique()
    logger.info("GraphST domains: %d (requested %d)", actual_n, n_domains)

    return {
        "method": "graphst",
        "n_domains": actual_n,
        "n_domains_requested": n_domains,
        "clustering": clustering_name,
        "device": str(device),
        "domain_counts": adata.obs["spatial_domain"].value_counts().to_dict(),
    }


def identify_domains_banksy(
    adata,
    *,
    n_domains: int | None = None,
    resolution: float = 0.7,
    lambda_param: float = 0.2,
    num_neighbours: int = 15,
    max_m: int = 1,
    pca_dims: int = 20,
) -> dict:
    """BANKSY — spatial feature augmentation for domain identification.

    Augments gene expression with neighborhood-averaged expression and
    azimuthal Gabor filters.  BANKSY expects **non-negative normalized
    expression** (library-size normalized, *not* z-scored).  The function
    restores raw counts from ``adata.layers["counts"]`` or ``adata.raw``
    and applies ``normalize_total`` without ``log1p`` so that all values
    remain >= 0.
    """
    from .dependency_manager import require

    require("banksy", feature="BANKSY spatial domain identification")

    from banksy.embed_banksy import generate_banksy_matrix
    from banksy.initialize_banksy import initialize_banksy

    logger.info("Running BANKSY (lambda=%.2f, n_domains=%s, resolution=%.2f) ...", lambda_param, n_domains, resolution)

    # --- Prepare non-negative normalized expression -------------------------
    # BANKSY requires non-negative values; z-score scaling (sc.pp.scale)
    # introduces negatives and must NOT be used here.
    adata_work = adata.copy()

    if "counts" in adata.layers:
        logger.info(
            "Restoring raw counts from adata.layers['counts'] and applying "
            "library-size normalization (non-negative) for BANKSY"
        )
        adata_work.X = adata.layers["counts"].copy()
        sc.pp.normalize_total(adata_work, target_sum=1e4)
    elif adata.raw is not None:
        logger.info(
            "Restoring raw counts from adata.raw and applying "
            "library-size normalization (non-negative) for BANKSY"
        )
        raw_ad = adata.raw.to_adata()
        adata_work.X = raw_ad.X.copy()
        sc.pp.normalize_total(adata_work, target_sum=1e4)
    else:
        # Fallback: use adata.X as-is but clip any negative values
        import scipy.sparse as sp

        x = adata_work.X
        x_min = x.min() if not sp.issparse(x) else x.data.min() if x.nnz > 0 else 0.0
        if x_min < 0:
            logger.warning(
                "BANKSY expects non-negative expression but detected min=%.4f. "
                "Clipping negatives to 0. For best results, ensure "
                "adata.layers['counts'] or adata.raw contains raw counts.",
                x_min,
            )
            if sp.issparse(x):
                adata_work.X = x.maximum(0)
            else:
                adata_work.X = np.maximum(x, 0)
        else:
            logger.info("Using adata.X as-is for BANKSY (non-negative, min=%.4f)", x_min)

    # Dramatically accelerate memory and processing by stripping down to highly-variable genes
    if "highly_variable" in adata.var.columns and adata.var["highly_variable"].sum() > 0:
        n_hvg = adata.var["highly_variable"].sum()
        logger.info("Subsetting to %d HVGs to accelerate BANKSY matrix generation", n_hvg)
        adata_work = adata_work[:, adata.var["highly_variable"]].copy()
    else:
        logger.warning("No HVG mask found. BANKSY will augment all %d genes, memory footprint may be extreme.", adata_work.n_vars)

    spatial_key = get_spatial_key(adata_work)
    if spatial_key is None:
        raise ValueError("BANKSY requires spatial coordinates in obsm")
    if spatial_key != "spatial":
        adata_work.obsm["spatial"] = adata_work.obsm[spatial_key]

    coord_keys = ("x", "y", "spatial")

    banksy_dict = initialize_banksy(
        adata_work,
        coord_keys=coord_keys,
        num_neighbours=num_neighbours,
        max_m=max_m,
        plt_edge_hist=False,
        plt_nbr_weights=False,
        plt_theta=False,
    )

    _, banksy_matrix = generate_banksy_matrix(
        adata_work, banksy_dict,
        lambda_list=[lambda_param],
        max_m=max_m, verbose=False,
    )

    sc.pp.pca(banksy_matrix, n_comps=pca_dims)

    # Smart Routing: Exact cluster count vs Graph-based discovery
    if n_domains is not None and n_domains > 0:
        logger.info("BANKSY: n_domains=%d specified, forcing extraction via tied-GMM fallback", n_domains)
        from sklearn.mixture import GaussianMixture
        from sklearn.cluster import KMeans
        
        embedding = banksy_matrix.obsm["X_pca"]
        try:
            gmm = GaussianMixture(n_components=n_domains, covariance_type="tied", random_state=42, reg_covar=1e-3)
            labels = gmm.fit_predict(embedding)
            cluster_name = "gmm_tied"
        except Exception as e:
            logger.warning("GMM failed (%s), shifting BANKSY segmentation to KMeans", e)
            kmeans = KMeans(n_clusters=n_domains, random_state=42, n_init=10)
            labels = kmeans.fit_predict(embedding)
            cluster_name = "kmeans"
            
        banksy_matrix.obs["banksy_cluster"] = pd.Categorical(labels.astype(str))
    else:
        logger.info("BANKSY: No fixed n_domains specified, running heuristic graph clustering (Leiden mode)")
        sc.pp.neighbors(banksy_matrix, use_rep="X_pca", n_neighbors=num_neighbours)
        sc.tl.leiden(banksy_matrix, resolution=resolution, flavor="igraph", key_added="banksy_cluster")
        n_domains = banksy_matrix.obs["banksy_cluster"].nunique()
        cluster_name = "leiden"

    adata.obs["spatial_domain"] = banksy_matrix.obs["banksy_cluster"].values
    adata.obsm["X_banksy_pca"] = banksy_matrix.obsm["X_pca"]

    actual_n = adata.obs["spatial_domain"].nunique()
    logger.info("BANKSY domains identified: %d", actual_n)

    return {
        "method": "banksy",
        "n_domains": actual_n,
        "n_domains_requested": n_domains,
        "lambda": lambda_param,
        "clustering": cluster_name,
        "resolution": resolution,
        "num_neighbours": num_neighbours,
        "original_features": adata_work.n_vars,
        "banksy_features": banksy_matrix.n_vars,
        "domain_counts": adata.obs["spatial_domain"].value_counts().to_dict(),
    }


# ---------------------------------------------------------------------------
# Method dispatch
# ---------------------------------------------------------------------------


def dispatch_method(method: str, adata, **kwargs) -> dict:
    """Route to the correct domain identification function.

    Parameters
    ----------
    method : str
        One of :data:`SUPPORTED_METHODS`.
    adata : AnnData
        Preprocessed spatial data.
    **kwargs
        Passed to the chosen method function.

    Returns
    -------
    dict
        Summary with keys: method, n_domains, domain_counts, ...
    """
    _DISPATCH = {
        "leiden": identify_domains_leiden,
        "louvain": identify_domains_louvain,
        "spagcn": identify_domains_spagcn,
        "stagate": identify_domains_stagate,
        "graphst": identify_domains_graphst,
        "banksy": identify_domains_banksy,
    }

    func = _DISPATCH.get(method)
    if func is None:
        raise ValueError(f"Unknown method: {method}. Choose from {SUPPORTED_METHODS}")

    # Filter kwargs to only pass what the function accepts
    import inspect
    sig = inspect.signature(func)
    valid_kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
    return func(adata, **valid_kwargs)
