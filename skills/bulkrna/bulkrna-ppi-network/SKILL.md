---
name: bulkrna-ppi-network
description: Load when querying STRING for the protein-protein interaction neighborhood of a bulk
  RNA-seq DEG list and finding hub genes. Skip when pathway enrichment of the same list (use bulkrna-enrichment);
  de novo co-expression network discovery (use bulkrna-coexpression).
trigger: PPI, protein interaction, STRING, network, hub gene, interactome
tags:
- bulkrna
- PPI
- STRING
- network
- hub-genes
- protein-interaction
---

# Bulk RNA-seq Protein-Protein Interaction Network (`bulkrna-ppi-network`)

## 1. Method Decision Matrix & When to Use

Select the protein-protein interaction (PPI) database query and graph centrality algorithm based on your biological question:

| Analytical Goal | Method / Graph Metric | Upstream Package / API | Why |
|---|---|---|---|
| **Retrieve physical & functional interactome** for a DEG list ($20\text{--}500$ genes) | STRING REST API (`json/network`) | `requests` (`https://string-db.org/api`) | Queries experimentally validated, database-curated, and co-expression PPI edges with combined confidence scores ($0\text{--}1000$) |
| **Identify local intramodular bottleneck / hub proteins** | Degree + Betweenness + Closeness / PageRank centrality | `networkx` or `bulkrna-ppi-network` kernel | Ranks proteins that bridge signaling subnetworks (`betweenness`) alongside high-degree physical hubs |
| **Discover densely connected protein complexes** | Community detection (Louvain / Leiden / MCODE) | `networkx.community.louvain_communities` | Partitions large PPI graphs into discrete macromolecular complexes or signaling modules |

## 2. How to Run (Idiomatic Upstream Patterns)

### Pattern A: Idiomatic STRING API Query + Graph Centrality Analysis

```python
import pandas as pd
import requests
import networkx as nx

# 1. Query STRING REST API for human (species=9606) interactions at medium-high confidence (>= 400)
deg_genes = ["TP53", "MDM2", "CDKN1A", "ATM", "CHEK2", "BRCA1", "MYC", "BCL2"]
resp = requests.post(
    "https://string-db.org/api/json/network",
    data={
        "identifiers": "%0d".join(deg_genes),
        "species": 9606,
        "required_score": 400,
        "caller_identity": "bioagent_bulkrna_ppi",
    },
    timeout=30,
)
resp.raise_for_status()
edges_raw = pd.DataFrame(resp.json())
edges = edges_raw.rename(columns={"preferredName_A": "gene_a", "preferredName_B": "gene_b"})[["gene_a", "gene_b", "score"]]

# 2. Build undirected PPI graph and compute node centralities
G = nx.from_pandas_edgelist(edges, source="gene_a", target="gene_b", edge_attr="score")
G.add_nodes_from(deg_genes)  # preserve isolated query DEGs
deg_c = nx.degree_centrality(G)
btw_c = nx.betweenness_centrality(G, normalized=True)
cls_c = nx.closeness_centrality(G)
```

### Pattern B: Offline PPI Centrality & Hub Ranking via `use_skill("bulkrna-ppi-network")`

```python
# Calling use_skill("bulkrna-ppi-network") auto-injects kernel.py helpers into the python kernel:
centrality_df = compute_ppi_centrality(edges, genes=deg_genes)
top_hubs = rank_hub_genes(centrality_df, top_n=20)
```

## 3. Output Format & Verification Contract

- **Interaction Edges Table** (`tables/interaction_edges.csv`):
  - Columns: `gene_a`, `gene_b`, `score` (normalized confidence score in $[0, 1]$).
- **Node Centrality Table** (`tables/node_centrality.csv`):
  - Columns: `gene`, `degree`, `betweenness`, `closeness`, `hub_score`.
- **Top Hub Genes Table** (`tables/hub_genes.csv`):
  - Top-ranked nodes sorted descending by `hub_score` (and `degree`).
- **Verification Invariants**:
  - Self-loops (`gene_a == gene_b`) and duplicate undirected edges (`(A, B)` vs. `(B, A)`) are deduplicated before degree counting.
  - All centrality metrics are finite and bounded in $[0, 1]$.

## 4. Gotchas & Troubleshooting

1. **STRING Confidence Score Scale (`0–1000` in API Parameters vs. `0.0–1.0` in JSON Output)**:
   - The STRING API parameter `required_score` expects an integer in $[0, 1000]$ (`150` = low, `400` = medium, `700` = high, `900` = highest), whereas the returned JSON `score` field is a float in $[0.0, 1.0]$. Never pass `required_score=0.4` to the API.
2. **Induced Subgraph vs. Extended 1-Hop Neighborhood**:
   - By default, STRING's `network` endpoint returns interactions strictly among your query genes, unless `add_nodes` is specified. If a query gene list is small ($< 15$ genes), the induced graph may be disconnected; either lower `required_score` to `400` or include first-shell interactors.
3. **Literature & Study-Bias Hubs (`TP53`, `TNF`, `EGFR`, `AKT1`)**:
   - Heavily studied cancer and immune proteins have thousands of curated interactions in STRING. Always interpret PPI hub scores alongside expression effect sizes (`log2FoldChange` and `padj` from `bulkrna-de`) rather than relying on topological degree alone.
4. **Taxonomy ID Must Match Organism (`9606` Human vs. `10090` Mouse)**:
   - Querying mouse gene symbols (`Trp53`, `Mdm2`) against `species=9606` returns empty or mismatched hits. Specify `species=10090` for *Mus musculus*.

## Dependencies

`numpy`, `pandas`, `matplotlib`, `requests`
