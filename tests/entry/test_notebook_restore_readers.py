"""Fixed readers keep whole groups private until the owner permits publication."""

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from omicsclaw.skillenv import (
    checkpoint_restore,
    checkpoint_science,
    notebook_checkpoint,
)
from tests.entry.test_notebook_scientific_inspector import request as inspect_request

ROOT = Path(checkpoint_restore.__file__).parent


def prepared_source(tmp_path):
    import anndata as ad
    import numpy as np
    import pandas as pd
    from scipy.sparse import csr_matrix

    data = ad.AnnData(
        csr_matrix(np.arange(12).reshape(6, 2)),
        obs=pd.DataFrame({"group": ["A"] * 6}, index=[f"c{i}" for i in range(6)]),
        var=pd.DataFrame(index=["g1", "g2"]),
    )
    data.layers["counts"] = data.X.copy()
    data = data[[1, 3, 5]].copy()
    data.obs["cluster"] = ["one", "two", "one"]
    data.obsm["X_umap"] = np.arange(6).reshape(3, 2)
    table = pd.DataFrame({"gene": ["g1", "g2"], "p": [0.1, 0.2]}, index=["g1", "g2"])
    namespace = {"data": data, "table": table}
    source = tmp_path / "source"
    source.mkdir()
    entries = [
        {"logicalKey": name, "symbol": name, "adapter": adapter}
        for name, adapter in (("data", "anndata"), ("table", "python_table"))
    ]
    notebook_checkpoint.save(
        namespace,
        {"objects": entries, "maxFileBytes": 1000000, "maxBytes": 2000000},
        source,
    )
    checkpoint_science.runtime_evidence(source)
    manifest = json.loads((source / "saved.json").read_text())
    files = []
    for item in manifest["objects"]:
        path = source / item["file"]
        files.append(
            {
                **item,
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    request = {
        "token": "__omicsclaw_restore_fixture",
        "files": files,
        "runtimeEvidence": json.loads((source / "runtime.json").read_text()),
        "inspection": [
            {
                "logicalKey": entry["logicalKey"],
                "request": inspect_request(entry["adapter"], symbol=entry["symbol"]),
            }
            for entry in entries
        ],
        "maxStagingBytes": 1000000,
    }
    return namespace, request


def test_real_whole_python_group_is_private_then_matches_source(tmp_path):
    import numpy as np
    import pandas as pd

    source, request = prepared_source(tmp_path)
    original_id = id(source["data"])
    original_obs = source["data"].obs.copy(deep=True)
    target, directory = {}, tmp_path / "target"
    directory.mkdir()
    checkpoint_restore.prepare(target, request, directory)
    assert set(target) == {request["token"]}
    assert (
        json.loads((directory / "data/inspection.json").read_text())["status"] == "ok"
    )
    assert id(source["data"]) == original_id
    pd.testing.assert_frame_equal(source["data"].obs, original_obs)
    checkpoint_restore.commit(target, request["token"], directory)
    assert set(target) == {"data", "table"}
    assert id(target["data"]) != original_id
    for field in ("X",):
        np.testing.assert_array_equal(
            getattr(target["data"], field).toarray(),
            getattr(source["data"], field).toarray(),
        )
    np.testing.assert_array_equal(
        target["data"].layers["counts"].toarray(),
        source["data"].layers["counts"].toarray(),
    )
    np.testing.assert_array_equal(
        target["data"].obsm["X_umap"], source["data"].obsm["X_umap"]
    )
    pd.testing.assert_frame_equal(target["data"].obs, source["data"].obs)
    pd.testing.assert_frame_equal(target["data"].var, source["data"].var)
    pd.testing.assert_frame_equal(target["table"], source["table"])
    assert json.loads((directory / "committed.json").read_text()) == {
        "token": request["token"]
    }
    with pytest.raises(ValueError, match="restore_target_changed"):
        checkpoint_restore.commit(target, request["token"], directory)


@pytest.mark.parametrize(
    "fault", ["version", "hash", "symbol", "inspection", "commit_collision"]
)
def test_reader_failure_does_not_publish_partial_group(tmp_path, fault):
    source, request = prepared_source(tmp_path)
    request = copy.deepcopy(request)
    target, directory = {}, tmp_path / "target"
    directory.mkdir()
    if fault == "version":
        request["runtimeEvidence"]["packages"]["pandas"]["version"] = "0.0.0-unverified"
    elif fault == "hash":
        request["files"][1]["sha256"] = "0" * 64
    elif fault == "symbol":
        target["table"] = "existing sentinel"
    elif fault == "inspection":
        request["maxStagingBytes"] = 1
    if fault == "commit_collision":
        checkpoint_restore.prepare(target, request, directory)
        target["table"] = "existing sentinel"
        with pytest.raises(ValueError, match="restore_target_changed"):
            checkpoint_restore.commit(target, request["token"], directory)
    else:
        with pytest.raises((ValueError, OSError)):
            checkpoint_restore.prepare(target, request, directory)
    assert "data" not in target
    assert target.get("table") in (None, "existing sentinel")
    assert not (directory / "committed.json").exists()
    assert source["data"].obs_names.tolist() == ["c1", "c3", "c5"]


def test_real_r_group_preserves_fitted_dds_and_table(tmp_path):
    r = Path(sys.executable).with_name("Rscript")
    if not r.exists():
        pytest.skip("Explicit scientific Rscript is unavailable")
    code = r"""args<-commandArgs(trailingOnly=TRUE);root<-args[[1]];directory<-args[[2]];python<-args[[3]]
source(file.path(root,'notebook_checkpoint.R'));source(file.path(root,'checkpoint_science.R'));source(file.path(root,'checkpoint_restore.R'))
suppressPackageStartupMessages(library(DESeq2))
set.seed(1); m<-matrix(as.integer(rnbinom(1000,size=2,mu=100)),nrow=200)
rownames(m)<-paste0('g',seq_len(nrow(m)));colnames(m)<-paste0('s',seq_len(ncol(m)))
dds<-DESeqDataSetFromMatrix(m,data.frame(group=factor(c('a','a','b','b','b')),row.names=colnames(m)),~group)
dds<-DESeq(dds,quiet=TRUE,fitType='mean');res<-as.data.frame(results(dds));before<-serialize(list(dds,res),NULL)
saved<-file.path(directory,'saved');dir.create(saved)
objects<-list(list(logicalKey='dds',symbol='dds',adapter='r_rds'),list(logicalKey='res',symbol='res',adapter='r_rds'))
checkpoint_save(.GlobalEnv,list(objects=objects,maxFileBytes=2000000,maxBytes=4000000),saved,python,file.path(root,'checkpoint_io.py'))
checkpoint_runtime_evidence(saved)
files<-jsonlite::fromJSON(file.path(saved,'saved.json'),simplifyVector=FALSE)$objects
files<-lapply(files,function(item){item$path<-file.path(saved,item$file);item$md5<-unname(tools::md5sum(item$path));item})
inspection<-lapply(objects,function(item) list(logicalKey=item$logicalKey,request=list(schemaVersion=1L,symbol=item$symbol,adapter=item$adapter,rowIdentity=list(kind='index'),columnIdentity=list(kind='index'),tableAxis='row',declaredMatrix=list(assay=if(item$symbol=='dds') 'counts' else NULL,layer=NULL,semantics=if(item$symbol=='dds') 'raw_counts' else 'unknown'))))
request<-list(token='__omicsclaw_restore_fixture',files=files,runtimeEvidence=jsonlite::fromJSON(file.path(saved,'runtime.json'),simplifyVector=FALSE),inspection=inspection,maxStagingBytes=1000000)
target<-new.env(parent=emptyenv());out<-file.path(directory,'target');dir.create(out)
restore_prepare(target,request,out,root)
stopifnot(identical(ls(target,all.names=TRUE),request$token),identical(serialize(list(dds,res),NULL),before))
restore_commit(target,request$token,out,root)
stopifnot(identical(ls(target),c('dds','res')),identical(counts(target$dds),counts(dds)),identical(as.data.frame(colData(target$dds)),as.data.frame(colData(dds))),identical(target$res,res),identical(results(target$dds),results(dds)),identical(assays(target$dds),assays(dds)))
bad<-file.path(directory,'incompatible');dir.create(bad);request$runtimeEvidence$packages$DESeq2$version<-'0.0.0'
fresh<-new.env(parent=emptyenv());failed<-tryCatch({restore_prepare(fresh,request,bad,root);FALSE},error=function(e) grepl('restore_dependency_incompatible',conditionMessage(e),fixed=TRUE))
stopifnot(failed,length(ls(fresh,all.names=TRUE))==0L,identical(serialize(list(dds,res),NULL),before))
"""
    result = subprocess.run(
        [str(r), "--vanilla", "-", str(ROOT), str(tmp_path), sys.executable],
        input=code,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
