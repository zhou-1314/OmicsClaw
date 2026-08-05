from __future__ import annotations

import importlib.util
from pathlib import Path

import h5py
import numpy as np


ADAPTER = Path(__file__).with_name("scagentbench_paga.py")


def _load_adapter():
    spec = importlib.util.spec_from_file_location("scagentbench_paga_adapter", ADAPTER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_legacy_paul15_loader_uses_the_suite_hdf5_layout(tmp_path):
    module = _load_adapter()
    source = tmp_path / "paul15.h5"
    with h5py.File(source, "w") as handle:
        handle["data.debatched"] = np.array(
            [[1, 2, 3], [0, 1, 0], [4, 0, 2]], dtype=np.float32
        )
        handle["data.debatched_rownames"] = np.array(
            [b"GeneA;alias", b"GeneB", b"GeneC"]
        )
        handle["data.debatched_colnames"] = np.array([b"c1", b"c2", b"c3"])
        handle["cluster.id"] = np.array([[1], [2], [3]], dtype=int)
        handle["info.genes_strings"] = np.array([b"GeneA", b"GeneC"])

    adata = module._load_legacy_paul15(source)
    assert adata.shape == (3, 2)
    assert adata.obs_names.tolist() == ["c1", "c2", "c3"]
    assert adata.var_names.tolist() == ["GeneA", "GeneC"]
    assert adata.obs["paul15_clusters"].tolist() == ["1Ery", "2Ery", "3Ery"]
    assert adata.uns["iroot"] == 840


def test_adapter_retains_native_metric_but_uses_aligned_scientific_checks():
    module = _load_adapter()
    matrix = np.array(
        [
            [0.0, 0.8, 0.0],
            [0.8, 0.0, 0.4],
            [0.0, 0.4, 0.0],
        ]
    )
    assert module._aligned_cosine(matrix, matrix) == 1.0
    assert module._edge_f1(matrix, matrix) == 1.0
    assert module._native_pairwise_mean(matrix, matrix) < 1.0
