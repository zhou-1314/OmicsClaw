"""Real scientific objects exercise standalone Python and R inspection files."""

import hashlib
import io
import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).parents[2] / "omicsclaw/skillenv"


@pytest.fixture
def functions():
    return (
        runpy.run_path(str(SOURCE / "notebook_identity.py")),
        runpy.run_path(str(SOURCE / "notebook_inspect.py")),
    )


def request(adapter="anndata", **updates):
    value = {
        "schemaVersion": 1,
        "symbol": "data",
        "adapter": adapter,
        "rowIdentity": {"kind": "index"},
        "columnIdentity": {"kind": "index"},
        "tableAxis": "row",
        "declaredMatrix": {"assay": None, "layer": None, "semantics": "unknown"},
    }
    value.update(updates)
    return value


def inspect(functions, tmp_path, data, specification=None):
    codec, inspector = functions
    namespace = {"data": data}
    before = dict(namespace)
    result = inspector["inspect_object"](
        namespace, specification or request(), tmp_path, codec
    )
    assert namespace == before
    assert json.loads((tmp_path / "inspection.json").read_text()) == result
    assert not list(tmp_path.glob("*.tmp"))
    return result


def read_ids(functions, directory, axis):
    with (directory / axis["idsFile"]).open("rb") as stream:
        return list(functions[0]["read_rows"](stream, axis["keys"]))


def table_rows(directory):
    return [
        json.loads(line)
        for line in (directory / "table.rows.jsonl").read_text().splitlines()
    ]


def test_anndata_composite_axis_counts_and_metadata_are_unchanged(functions, tmp_path):
    ad = pytest.importorskip("anndata")
    pd = pytest.importorskip("pandas")
    np = pytest.importorskip("numpy")
    sparse = pytest.importorskip("scipy.sparse")
    obs = pd.DataFrame(
        {
            "sample": ["S1"] * 50 + ["S2"] * 51,
            "barcode": [f"AA{i}" for i in range(50)] + [f"AA{i}" for i in range(51)],
            "condition": ["control"] * 50 + ["case"] * 51,
        },
        index=[f"cell{i}" for i in range(101)],
    )
    data = ad.AnnData(
        sparse.csr_matrix(np.ones((101, 3), dtype=np.float32)),
        obs=obs,
        var=pd.DataFrame(
            {"gene_type": ["coding", "coding", "lncRNA"]}, index=["g1", "g2", "汉字"]
        ),
    )
    data.layers["counts"] = sparse.csr_matrix(
        np.arange(303, dtype=np.int32).reshape(101, 3)
    )
    original = data.copy()
    result = inspect(
        functions,
        tmp_path,
        data,
        request(
            rowIdentity={"kind": "columns", "names": ["sample", "barcode"]},
            declaredMatrix={
                "assay": None,
                "layer": "counts",
                "semantics": "raw_counts",
            },
        ),
    )
    assert result["status"] == "ok" and result["shape"] == [101, 3]
    assert [axis["role"] for axis in result["axes"]] == ["obs", "var"]
    assert result["matrix"] == {
        "assay": None,
        "layer": "counts",
        "exists": True,
        "axesAligned": True,
        "semantics": "raw_counts",
        "semanticsEvidence": "declared",
        "reason": "",
    }
    ids = read_ids(functions, tmp_path, result["axes"][0])
    assert ids[0] == ["S1", "AA0"] and ids[50] == ["S2", "AA0"] and len(ids) == 101
    assert table_rows(tmp_path)[50] == {
        "id": ["S2", "AA0"],
        "values": ["S2", "AA0", "case"],
    }
    assert (data.X != original.X).nnz == 0
    assert (data.layers["counts"] != original.layers["counts"]).nnz == 0
    assert data.layers["counts"].dtype == original.layers["counts"].dtype
    pd.testing.assert_frame_equal(data.obs, original.obs)
    pd.testing.assert_frame_equal(data.var, original.var)


def test_column_table_follows_var_axis_and_missing_layer_does_not_fallback(
    functions, tmp_path
):
    ad = pytest.importorskip("anndata")
    np = pytest.importorskip("numpy")
    pd = pytest.importorskip("pandas")
    data = ad.AnnData(
        np.ones((2, 3)),
        obs=pd.DataFrame(index=["c1", "c2"]),
        var=pd.DataFrame({"kind": ["a", "b", "c"]}, index=["g1", "g2", "g3"]),
    )
    result = inspect(
        functions,
        tmp_path,
        data,
        request(
            tableAxis="column",
            declaredMatrix={
                "assay": None,
                "layer": "missing",
                "semantics": "raw_counts",
            },
        ),
    )
    assert (
        result["matrix"]["exists"] is False and result["matrix"]["layer"] == "missing"
    )
    assert result["matrix"]["semanticsEvidence"] == "declared"
    assert result["table"]["rowCount"] == 3
    assert table_rows(tmp_path)[2] == {"id": ["g3"], "values": ["g3", "c"]}


def test_dataframe_unicode_nonfinite_and_hostile_metadata_are_bounded(
    functions, tmp_path
):
    pd = pytest.importorskip("pandas")
    np = pytest.importorskip("numpy")

    class NoRepr:
        def __repr__(self):
            raise AssertionError("User repr must never run")

    data = pd.DataFrame(
        {
            "sample": ["S1", "S2"],
            "barcode": ["é", "é"],
            "value": [np.inf, np.nan],
            "long": ["汉" * 500, "ok"],
            "object": [NoRepr(), NoRepr()],
        }
    )
    result = inspect(
        functions,
        tmp_path,
        data,
        request(
            "python_table",
            rowIdentity={"kind": "columns", "names": ["sample", "barcode"]},
        ),
    )
    assert result["matrix"]["semanticsEvidence"] == "unknown"
    assert result["shape"] == [2, 5]
    rows = table_rows(tmp_path)
    assert rows[0] == {"id": ["S1", "é"], "values": ["S1", "é", None, None, None]}
    assert rows[1]["id"] == ["S2", "é"]
    assert "nonfinite_metadata_omitted" in result["warnings"]
    assert "unsupported_metadata_omitted" in result["warnings"]


@pytest.mark.parametrize("values", [[1, 2], ["A\0B", "ok"], ["a" * 513, "ok"]])
def test_invalid_identity_never_becomes_coerced_or_truncated_view(
    functions, tmp_path, values
):
    pd = pytest.importorskip("pandas")
    data = pd.DataFrame({"id": values})
    result = inspect(
        functions,
        tmp_path,
        data,
        request("python_table", rowIdentity={"kind": "columns", "names": ["id"]}),
    )
    assert result["status"] == "ok" and result["axes"][0]["count"] == 2
    assert result["axes"][0]["idsFile"] is None and result["axes"][0]["reason"]
    assert result["table"] is None


def test_duplicate_axes_are_preserved_for_owner_uniqueness_validation(
    functions, tmp_path
):
    pd = pytest.importorskip("pandas")
    data = pd.DataFrame({"id": ["AA", "AA"]})
    result = inspect(
        functions,
        tmp_path,
        data,
        request("python_table", rowIdentity={"kind": "columns", "names": ["id"]}),
    )
    rows = read_ids(functions, tmp_path, result["axes"][0])
    assert rows == [["AA"], ["AA"]]
    with pytest.raises(ValueError, match="duplicate_identity"):
        functions[0]["digest_rows"](["id"], rows, ordered=False)


def test_table_has_byte_column_and_string_budgets(functions, tmp_path):
    pd = pytest.importorskip("pandas")
    data = pd.DataFrame({f"field{i}": ["x" * 1024] for i in range(25)}, index=["row"])
    result = inspect(functions, tmp_path, data, request("python_table"))
    assert len(result["table"]["columns"]) == 20
    assert len((tmp_path / "table.rows.jsonl").read_bytes()) <= 16384
    assert table_rows(tmp_path)[0]["values"] == ["row"] + [None] * 19
    assert set(result["warnings"]) >= {
        "metadata_columns_limited",
        "metadata_row_limited",
    }


def test_unsupported_object_never_reads_shape_property(functions, tmp_path):
    class Unknown:
        @property
        def shape(self):
            raise AssertionError("Unknown property must never run")

    result = inspect(functions, tmp_path, Unknown(), request("python_table"))
    assert result == {
        "schemaVersion": 1,
        "status": "error",
        "reason": "unsupported_type",
    }


def test_backed_anndata_is_explicitly_unsupported(functions, tmp_path):
    ad = pytest.importorskip("anndata")
    np = pytest.importorskip("numpy")
    file = tmp_path / "input.h5ad"
    ad.AnnData(np.ones((2, 3))).write_h5ad(file)
    before = hashlib.sha256(file.read_bytes()).hexdigest()
    data = ad.read_h5ad(file, backed="r")
    try:
        result = inspect(functions, tmp_path, data)
        assert result["reason"] == "backed_anndata_unsupported"
        assert hashlib.sha256(file.read_bytes()).hexdigest() == before
    finally:
        data.file.close()


def test_selection_101_members_is_canonical_and_unique(functions, tmp_path):
    codec, inspector = functions
    rows = [["S1", f"汉字{i}"] for i in range(101)]
    result = inspector["write_selection"](
        ["sample", "barcode"], iter(rows), tmp_path, codec
    )
    assert result["count"] == 101
    assert set(result) == {"schemaVersion", "file", "keys", "count"}
    assert (
        json.loads((tmp_path / result["file"]).with_suffix(".json").read_text())
        == result
    )
    raw = (tmp_path / result["file"]).read_bytes()
    assert list(codec["read_rows"](io.BytesIO(raw), result["keys"])) == rows
    for invalid in ([], [["S1", "AA"], ["S1", "AA"]], [["S1", 7]]):
        previous = sorted(path.name for path in tmp_path.iterdir())
        with pytest.raises(ValueError):
            inspector["write_selection"](
                ["sample", "barcode"], invalid, tmp_path, codec
            )
        assert sorted(path.name for path in tmp_path.iterdir()) == previous


def test_selected_real_r_inspects_matrix_frame_and_deseq_dataset(functions, tmp_path):
    executable = Path(sys.executable).with_name("Rscript")
    if not executable.is_file():
        pytest.skip("Selected scientific Python has no adjacent Rscript")
    script = r"""
args <- commandArgs(trailingOnly=TRUE)
private <- new.env(parent=baseenv())
sys.source(file.path(args[[1]], "notebook_identity.R"), envir=private)
sys.source(file.path(args[[1]], "notebook_inspect.R"), envir=private)
request <- function(...) {
  result <- list(schemaVersion=1L,symbol="data",adapter="r_rds",rowIdentity=list(kind="index"),columnIdentity=list(kind="index"),tableAxis="row",declaredMatrix=list(assay=NULL,layer=NULL,semantics="unknown"))
  for (name in names(list(...))) result[[name]] <- list(...)[[name]]
  result
}
run <- function(name, data, req=request()) {
  directory <- file.path(args[[2]], name)
  dir.create(directory)
  namespace <- new.env(parent=emptyenv())
  namespace$data <- data
  before <- serialize(data, NULL)
  result <- private$inspect_object(namespace, req, directory)
  stopifnot(identical(before, serialize(namespace$data, NULL)), identical(ls(namespace), "data"))
  result
}
stopifnot(requireNamespace("SummarizedExperiment", quietly=TRUE), requireNamespace("DESeq2", quietly=TRUE))
matrix <- matrix(seq_len(606L), nrow=101L, dimnames=list(paste0("gene",seq_len(101)),paste0("sample",seq_len(6))))
run("matrix", matrix)
frame <- data.frame(sample=c("S1","S2"),barcode=factor(c("é","é")),value=c(Inf,NaN),long=c(paste0(rep("汉",500),collapse=""),"ok"),row.names=c("r1","r2"))
run("frame", frame, request(rowIdentity=list(kind="columns",names=c("sample","barcode"))))
wide <- as.data.frame(setNames(rep(list(strrep("x",1024)),25),paste0("field",seq_len(25))),row.names="row")
run("wide",wide)
meta <- data.frame(condition=factor(c("control","control","control","case","case","case")),sample_id=colnames(matrix),row.names=colnames(matrix))
dds <- DESeq2::DESeqDataSetFromMatrix(matrix, meta, design=~condition)
SummarizedExperiment::rowData(dds)$gene_type <- rep("coding",101)
run("dds",dds,request(tableAxis="column",columnIdentity=list(kind="columns",names="sample_id"),declaredMatrix=list(assay="counts",layer=NULL,semantics="raw_counts")))
run("default_assay",dds)
run("missing_assay",dds,request(declaredMatrix=list(assay="missing",layer=NULL,semantics="raw_counts")))
run("missing_layer",dds,request(declaredMatrix=list(assay="counts",layer="missing",semantics="raw_counts")))
run("numeric_identity",frame,request(rowIdentity=list(kind="columns",names="value")))
run("unsupported",list(secret="private"))
run("missing_ids",unname(matrix))
duplicate <- data.frame(id=c("AA","AA"))
run("duplicates",duplicate,request(rowIdentity=list(kind="columns",names="id")))
path <- file.path(args[[2]],"active")
dir.create(path)
namespace <- new.env(parent=emptyenv())
makeActiveBinding("data", function() stop("must not execute"), namespace)
private$inspect_object(namespace,request(),path)
selection <- file.path(args[[2]],"selection")
dir.create(selection)
rows <- lapply(seq_len(101L),function(index)c("S1",paste0("汉字",index-1L)))
descriptor <- private$write_selection(c("sample","barcode"),rows,selection)
stopifnot(descriptor$count == 101L)
for (bad in list(list(),list(c("S1","AA"),c("S1","AA")),list(c(1,2)))) {
  before <- list.files(selection)
  result <- try(private$write_selection(c("sample","barcode"),bad,selection),silent=TRUE)
  stopifnot(inherits(result,"try-error"),identical(before,list.files(selection)))
}
connection <- rawConnection(raw(), "wb")
result <- try(private$identity_write_iter(connection, "id", 1000001L, function(i)stop("must not read")),silent=TRUE)
stopifnot(inherits(result,"try-error"),length(rawConnectionValue(connection))==0L)
close(connection)
run("invalid_request",matrix,request(schemaVersion=TRUE))
empty <- matrix(numeric(),0L,0L,dimnames=list(character(),character()))
run("empty",empty)
cat("SCIENTIFIC_INSPECTION_OK\n")
"""
    completed = subprocess.run(
        [str(executable), "--vanilla", "-e", script, str(SOURCE), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert "SCIENTIFIC_INSPECTION_OK" in completed.stdout
    outputs = {
        path.parent.name: json.loads(path.read_text())
        for path in tmp_path.glob("*/inspection.json")
    }
    assert outputs["matrix"]["shape"] == [101, 6]
    assert [axis["role"] for axis in outputs["matrix"]["axes"]] == ["row", "column"]
    assert outputs["matrix"]["matrix"]["semanticsEvidence"] == "unknown"
    dds = outputs["dds"]
    assert dds["status"] == "ok" and dds["type"] == "R.DESeqDataSet"
    assert [axis["role"] for axis in dds["axes"]] == ["gene", "sample"]
    assert dds["matrix"] == {
        "assay": "counts",
        "layer": None,
        "exists": True,
        "axesAligned": True,
        "semantics": "raw_counts",
        "semanticsEvidence": "declared",
        "reason": "",
    }
    assert table_rows(tmp_path / "dds")[3] == {
        "id": ["sample4"],
        "values": ["sample4", "case"],
    }
    assert outputs["default_assay"]["matrix"]["assay"] == "counts"
    for name in ("missing_assay", "missing_layer"):
        assert outputs[name]["matrix"]["exists"] is False
    assert outputs["missing_assay"]["matrix"]["assay"] == "missing"
    assert outputs["numeric_identity"]["axes"][0]["idsFile"] is None
    assert outputs["numeric_identity"]["table"] is None
    assert outputs["missing_ids"]["axes"][0]["count"] == 101
    assert outputs["missing_ids"]["table"] is None
    assert outputs["unsupported"] == {
        "schemaVersion": 1,
        "status": "error",
        "reason": "unsupported_type",
    }
    assert outputs["active"]["reason"] == "active_binding_unsupported"
    assert outputs["invalid_request"]["reason"] == "invalid_request"
    assert outputs["empty"]["shape"] == [0, 0]
    assert table_rows(tmp_path / "frame")[0] == {
        "id": ["S1", "é"],
        "values": ["S1", "é", None, None],
    }
    assert len(outputs["wide"]["table"]["columns"]) == 20
    assert len((tmp_path / "wide/table.rows.jsonl").read_bytes()) <= 16384
    duplicate = read_ids(
        functions, tmp_path / "duplicates", outputs["duplicates"]["axes"][0]
    )
    assert duplicate == [["AA"], ["AA"]]
    descriptor = json.loads(next((tmp_path / "selection").glob("*.json")).read_text())
    assert descriptor["count"] == 101 and descriptor["keys"] == ["sample", "barcode"]
    raw = (tmp_path / "selection" / descriptor["file"]).read_bytes()
    expected = [["S1", f"汉字{i}"] for i in range(101)]
    assert (
        list(functions[0]["read_rows"](io.BytesIO(raw), descriptor["keys"])) == expected
    )
    assert not list(tmp_path.rglob("*.tmp"))


def test_selection_limits_stop_stream_and_publish_nothing(
    functions, tmp_path, monkeypatch
):
    codec, inspector = functions
    namespace = codec["write_rows"].__globals__
    monkeypatch.setitem(namespace, "MAX_IDS", 2)
    visited = []

    def rows():
        for index in range(100):
            visited.append(index)
            yield [f"id{index}"]

    with pytest.raises(ValueError, match="identity_limit"):
        inspector["write_selection"](["id"], rows(), tmp_path, codec)
    assert visited == [0, 1, 2]
    assert list(tmp_path.iterdir()) == []
    monkeypatch.setitem(namespace, "MAX_IDS", 1_000_000)
    monkeypatch.setitem(namespace, "MAX_ID_BYTES", len(codec["header"](["id"])) + 8)
    with pytest.raises(ValueError, match="identity_limit"):
        inspector["write_selection"](["id"], [["0123456789"]], tmp_path, codec)
    assert list(tmp_path.iterdir()) == []


def test_failed_descriptor_publication_leaves_no_selection_artifact(
    functions, tmp_path, monkeypatch
):
    codec, inspector = functions
    os_module = inspector["_atomic_json"].__globals__["os"]
    replace = os_module.replace

    def fail_descriptor(source, target):
        if Path(target).suffix == ".json":
            raise OSError("simulated publish failure")
        return replace(source, target)

    monkeypatch.setattr(os_module, "replace", fail_descriptor)
    with pytest.raises(OSError, match="simulated publish failure"):
        inspector["write_selection"](["id"], [["one"]], tmp_path, codec)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "patch",
    [
        {"symbol": "data.shape"},
        {"schemaVersion": True},
        {"unexpected": "field"},
        {"rowIdentity": {"kind": "index", "extra": True}},
    ],
)
def test_invalid_request_does_not_publish_shape(functions, tmp_path, patch):
    result = inspect(functions, tmp_path, object(), request("python_table", **patch))
    assert result["status"] == "error" and "shape" not in result


def test_string_subclasses_do_not_execute_identity_methods(functions, tmp_path):
    class CustomString(str):
        def encode(self, *args, **kwargs):
            raise AssertionError("User encode must never run")

    with pytest.raises(ValueError, match="invalid_identity"):
        functions[1]["write_selection"](
            ["id"], [[CustomString("data")]], tmp_path, functions[0]
        )
    assert list(tmp_path.iterdir()) == []


def test_anndata_view_stays_a_view_without_dense_matrix_conversion(
    functions, tmp_path, monkeypatch
):
    ad = pytest.importorskip("anndata")
    pd = pytest.importorskip("pandas")
    np = pytest.importorskip("numpy")
    sparse = pytest.importorskip("scipy.sparse")
    original = ad.AnnData(
        sparse.csr_matrix(np.arange(12).reshape(4, 3)),
        obs=pd.DataFrame(index=["c1", "c2", "c3", "c4"]),
        var=pd.DataFrame(index=["g1", "g2", "g3"]),
    )
    subset = original[[0, 2], :]
    before_obs, before_var = subset.obs.copy(), subset.var.copy()

    def no_dense(*args, **kwargs):
        raise AssertionError("Expression matrix must not be densified")

    monkeypatch.setattr(sparse.csr_matrix, "toarray", no_dense)
    result = inspect(functions, tmp_path, subset)
    assert result["status"] == "ok" and result["shape"] == [2, 3]
    assert read_ids(functions, tmp_path, result["axes"][0]) == [["c1"], ["c3"]]
    assert subset.is_view
    pd.testing.assert_frame_equal(subset.obs, before_obs)
    pd.testing.assert_frame_equal(subset.var, before_var)
