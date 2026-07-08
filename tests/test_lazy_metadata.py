from pathlib import Path
from omicsclaw.skill.lazy_metadata import LazySkillMetadata

def test_lazy_metadata_loads_basic_info():
    skill_path = Path("skills/spatial/spatial-preprocess")
    lazy = LazySkillMetadata(skill_path)

    assert lazy.name == "spatial-preprocess"
    assert "Load when" in lazy.description
    assert "spatial transcriptomics" in lazy.description
    assert lazy.domain == "spatial"

def test_lazy_metadata_loads_full_on_demand():
    skill_path = Path("skills/spatial/spatial-preprocess")
    lazy = LazySkillMetadata(skill_path)

    # Basic info loaded immediately
    assert lazy.name == "spatial-preprocess"

    # Full metadata loaded on-demand
    full = lazy.get_full()
    assert "tags" in full
    assert "version" in full
    assert full["version"] == "0.6.0"


def test_lazy_metadata_loads_spatial_preprocess_param_hints():
    skill_path = Path("skills/spatial/spatial-preprocess")
    lazy = LazySkillMetadata(skill_path)

    hints = lazy.param_hints
    assert isinstance(hints, dict)
    assert "scanpy_standard" in hints
    assert "tissue" in hints["scanpy_standard"]["params"]
    assert "n_neighbors" in hints["scanpy_standard"]["params"]


def test_lazy_metadata_loads_param_hints():
    skill_path = Path("skills/spatial/spatial-genes")
    lazy = LazySkillMetadata(skill_path)

    hints = lazy.param_hints
    assert isinstance(hints, dict)
    assert "morans" in hints
    assert "sparkx" in hints
    assert "morans_n_neighs" in hints["morans"]["params"]
    assert "sparkx_option" in hints["sparkx"]["params"]


def test_lazy_metadata_loads_method_specific_flags():
    skill_path = Path("skills/spatial/spatial-genes")
    lazy = LazySkillMetadata(skill_path)

    flags = lazy.allowed_extra_flags
    assert "--morans-n-neighs" in flags
    assert "--spatialde-no-aeh" in flags
    assert "--sparkx-option" in flags
    assert "--flashs-n-rand-features" in flags


def test_lazy_metadata_loads_spatial_integration_param_hints():
    skill_path = Path("skills/spatial/spatial-integrate")
    lazy = LazySkillMetadata(skill_path)

    hints = lazy.param_hints
    assert isinstance(hints, dict)
    assert "harmony" in hints
    assert "bbknn" in hints
    assert "scanorama" in hints
    assert "harmony_theta" in hints["harmony"]["params"]
    assert "scanorama_batch_size" in hints["scanorama"]["params"]


def test_lazy_metadata_loads_spatial_integration_method_specific_flags():
    skill_path = Path("skills/spatial/spatial-integrate")
    lazy = LazySkillMetadata(skill_path)

    flags = lazy.allowed_extra_flags
    assert "--harmony-theta" in flags
    assert "--bbknn-neighbors-within-batch" in flags
    assert "--scanorama-batch-size" in flags


def test_lazy_metadata_loads_spatial_communication_param_hints():
    skill_path = Path("skills/spatial/spatial-communication")
    lazy = LazySkillMetadata(skill_path)

    hints = lazy.param_hints
    assert isinstance(hints, dict)
    assert "liana" in hints
    assert "cellphonedb" in hints
    assert "fastccc" in hints
    assert "cellchat_r" in hints
    assert "liana_expr_prop" in hints["liana"]["params"]
    assert "fastccc_lr_combination" in hints["fastccc"]["params"]


def test_lazy_metadata_loads_spatial_communication_method_specific_flags():
    skill_path = Path("skills/spatial/spatial-communication")
    lazy = LazySkillMetadata(skill_path)

    flags = lazy.allowed_extra_flags
    assert "--liana-expr-prop" in flags
    assert "--cellphonedb-iterations" in flags
    assert "--fastccc-min-percentile" in flags
    assert "--cellchat-prob-type" in flags


def test_lazy_metadata_loads_spatial_deconv_param_hints():
    skill_path = Path("skills/spatial/spatial-deconv")
    lazy = LazySkillMetadata(skill_path)

    hints = lazy.param_hints
    assert isinstance(hints, dict)
    assert "cell2location" in hints
    assert "rctd" in hints
    assert "spotlight" in hints
    assert "card" in hints
    assert "cell2location_n_cells_per_spot" in hints["cell2location"]["params"]
    assert "card_imputation" in hints["card"]["params"]


def test_lazy_metadata_loads_spatial_deconv_method_specific_flags():
    skill_path = Path("skills/spatial/spatial-deconv")
    lazy = LazySkillMetadata(skill_path)

    flags = lazy.allowed_extra_flags
    assert "--cell2location-n-cells-per-spot" in flags
    assert "--destvi-n-latent" in flags
    assert "--stereoscope-rna-epochs" in flags
    assert "--tangram-mode" in flags
    assert "--spotlight-weight-id" in flags
    assert "--card-num-grids" in flags


def test_lazy_metadata_loads_spatial_register_param_hints():
    skill_path = Path("skills/spatial/spatial-register")
    lazy = LazySkillMetadata(skill_path)

    hints = lazy.param_hints
    assert isinstance(hints, dict)
    assert "paste" in hints
    assert "stalign" in hints
    assert "paste_alpha" in hints["paste"]["params"]
    assert "stalign_a" in hints["stalign"]["params"]


def test_lazy_metadata_loads_spatial_register_method_specific_flags():
    skill_path = Path("skills/spatial/spatial-register")
    lazy = LazySkillMetadata(skill_path)

    flags = lazy.allowed_extra_flags
    assert "--slice-key" in flags
    assert "--paste-alpha" in flags
    assert "--paste-dissimilarity" in flags
    assert "--stalign-niter" in flags
    assert "--stalign-a" in flags


def test_lazy_metadata_loads_spatial_trajectory_param_hints():
    skill_path = Path("skills/spatial/spatial-trajectory")
    lazy = LazySkillMetadata(skill_path)

    hints = lazy.param_hints
    assert isinstance(hints, dict)
    assert "dpt" in hints
    assert "cellrank" in hints
    assert "palantir" in hints
    assert "dpt_n_dcs" in hints["dpt"]["params"]
    assert "cellrank_n_states" in hints["cellrank"]["params"]
    assert "palantir_num_waypoints" in hints["palantir"]["params"]


def test_lazy_metadata_loads_spatial_trajectory_method_specific_flags():
    skill_path = Path("skills/spatial/spatial-trajectory")
    lazy = LazySkillMetadata(skill_path)

    flags = lazy.allowed_extra_flags
    assert "--cluster-key" in flags
    assert "--root-cell-type" in flags
    assert "--dpt-n-dcs" in flags
    assert "--cellrank-frac-to-keep" in flags
    assert "--palantir-knn" in flags


# --- ADR 0030: skill `type` + `validation_level` ---------------------------

def test_lazy_metadata_reads_consensus_type():
    """A consensus shim declares `type: consensus` in its sidecar (ADR 0016)."""
    lazy = LazySkillMetadata(Path("skills/spatial/consensus-domains"))
    assert lazy.type == "consensus"


def test_lazy_metadata_defaults_type_leaf_and_validation_smoke_only():
    """A normal skill with no `type`/`validation_level` falls back to the
    conservative defaults (leaf / smoke-only)."""
    lazy = LazySkillMetadata(Path("skills/spatial/spatial-preprocess"))
    assert lazy.type == "leaf"
    assert lazy.validation_level == "smoke-only"


def test_lazy_metadata_clamps_unknown_type_and_validation_level(tmp_path):
    """Unknown enum values are clamped to the safe default rather than
    propagated, so a typo in a sidecar cannot mislabel a skill."""
    skill = tmp_path / "x"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: x\ndescription: d\n---\n# x\n", encoding="utf-8"
    )
    (skill / "parameters.yaml").write_text(
        "domain: d\ntype: bogus\nvalidation_level: bogus\n", encoding="utf-8"
    )
    lazy = LazySkillMetadata(skill)
    assert lazy.type == "leaf"
    assert lazy.validation_level == "smoke-only"


# --- acquisition P0: `origin` (provenance) + `lifecycle_status` ------------


def test_lazy_metadata_reads_origin_and_lifecycle_status_from_v2():
    """A hand-written v2 skill predates the acquisition flywheel: skill.yaml's
    own schema defaults (human/mvp) carry through unchanged."""
    lazy = LazySkillMetadata(Path("skills/spatial/spatial-preprocess"))
    assert lazy.origin == "human"
    assert lazy.lifecycle_status == "mvp"


def test_lazy_metadata_v1_fallback_defaults_origin_and_lifecycle(tmp_path):
    """v1 skills (SKILL.md + parameters.yaml) predate origin/lifecycle_status
    entirely — they must still resolve to the same defaults as their v2
    schema counterparts (human/mvp) rather than crashing on a missing key."""
    skill = tmp_path / "x"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: x\ndescription: d\n---\n# x\n", encoding="utf-8"
    )
    (skill / "parameters.yaml").write_text("domain: d\n", encoding="utf-8")
    lazy = LazySkillMetadata(skill)
    assert lazy.origin == "human"
    assert lazy.lifecycle_status == "mvp"
