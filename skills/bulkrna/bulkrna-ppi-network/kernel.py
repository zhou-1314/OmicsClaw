"""Lightweight kernel helpers for bulkrna-ppi-network (PPI graph centrality & hub ranking)."""
from __future__ import annotations
__all__ = ['compute_ppi_centrality', 'rank_hub_genes']

def compute_ppi_centrality(edges: pd.DataFrame, *, genes: list[str] | None=None) -> pd.DataFrame:
    """Compute degree, closeness, and Brandes betweenness centrality on an undirected PPI graph."""
    import pandas as pd
    from collections import deque
    adj: dict[str, set[str]] = {str(g): set() for g in genes or []}
    for _, row in edges.iterrows():
        u, v = (str(row['gene_a']), str(row['gene_b']))
        if u != v and (genes is None or (u in adj and v in adj)):
            adj.setdefault(u, set()).add(v)
            adj.setdefault(v, set()).add(u)
    nodes = sorted(adj.keys())
    n = len(nodes)
    if n == 0:
        return pd.DataFrame(columns=['gene', 'degree', 'betweenness', 'closeness', 'hub_score'])
    btw, cls = ({u: 0.0 for u in nodes}, {u: 0.0 for u in nodes})
    for s in nodes:
        stack, pred, sigma, dist = ([], {w: [] for w in nodes}, {w: 0.0 for w in nodes}, {w: -1 for w in nodes})
        sigma[s], dist[s], q = (1.0, 0, deque([s]))
        while q:
            v = q.popleft()
            stack.append(v)
            for w in adj[v]:
                if dist[w] < 0:
                    q.append(w)
                    dist[w] = dist[v] + 1
                if dist[w] == dist[v] + 1:
                    sigma[w] += sigma[v]
                    pred[w].append(v)
        reach = [d for d in dist.values() if d > 0]
        if reach and n > 1:
            cls[s] = len(reach) / (n - 1) * (len(reach) / sum(reach))
        delta = {w: 0.0 for w in nodes}
        while stack:
            w = stack.pop()
            for v in pred[w]:
                delta[v] += sigma[v] / sigma[w] * (1.0 + delta[w])
            if w != s:
                btw[w] += delta[w]
    scale = 1.0 / ((n - 1) * (n - 2)) if n > 2 else 0.0
    max_deg = max((len(adj[u]) for u in nodes), default=1) or 1
    max_btw = max((btw[u] * scale for u in nodes), default=1.0) or 1.0
    rows = [{'gene': u, 'degree': len(adj[u]), 'betweenness': btw[u] * scale, 'closeness': cls[u], 'hub_score': 0.5 * (len(adj[u]) / max_deg) + 0.5 * (btw[u] * scale / max_btw)} for u in nodes]
    return pd.DataFrame(rows).sort_values(['hub_score', 'degree'], ascending=False).reset_index(drop=True)

def rank_hub_genes(centrality_df: pd.DataFrame, *, top_n: int=20) -> pd.DataFrame:
    """Return top-N PPI hub genes ordered by composite hub_score and degree."""
    return centrality_df.sort_values(['hub_score', 'degree'], ascending=False).head(top_n).reset_index(drop=True)
