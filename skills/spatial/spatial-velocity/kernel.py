"""Lightweight kernel helpers for spatial-velocity (scVelo spatial velocity & kinetic summary)."""
from __future__ import annotations
__all__ = ['run_scvelo_spatial', 'velocity_summary']

def run_scvelo_spatial(adata, *, mode: str='stochastic', min_shared_counts: int=20, n_top_genes: int=2000, n_pcs: int=30, n_neighbors: int=30):
    """Estimate RNA velocity from spliced/unspliced layers and project onto spatial coordinates."""
    import scvelo as scv
    if 'spliced' not in adata.layers or 'unspliced' not in adata.layers:
        raise ValueError("adata.layers must contain both 'spliced' and 'unspliced' count matrices.")
    ad = adata.copy()
    scv.pp.filter_and_normalize(ad, min_shared_counts=min_shared_counts, n_top_genes=n_top_genes)
    scv.pp.moments(ad, n_pcs=n_pcs, n_neighbors=n_neighbors)
    if mode == 'dynamical':
        scv.tl.recover_dynamics(ad)
    scv.tl.velocity(ad, mode=mode)
    scv.tl.velocity_graph(ad)
    scv.tl.velocity_confidence(ad)
    if mode == 'dynamical':
        scv.tl.latent_time(ad)
    if 'spatial' in ad.obsm:
        scv.tl.velocity_embedding(ad, basis='spatial')
    return ad

def velocity_summary(adata, *, groupby: str='leiden') -> pd.DataFrame:
    """Summarize mean velocity length, coherence confidence, and latent time per spatial group."""
    cols = [c for c in ['velocity_length', 'velocity_confidence', 'latent_time'] if c in adata.obs]
    if not cols:
        raise ValueError('Run velocity estimation before calling velocity_summary.')
    return adata.obs.groupby(groupby, observed=True)[cols].mean().round(4).reset_index()
