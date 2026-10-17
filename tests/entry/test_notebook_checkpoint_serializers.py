"""Actual bounded files round-trip without implicit mutation of resident data."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from omicsclaw.skillenv import checkpoint_io, notebook_checkpoint

ROOT = Path(notebook_checkpoint.__file__).parent


def python(script, tmp_path):
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(ROOT), str(tmp_path)],
        capture_output=True,
        check=False,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr
    return result


def test_actual_anndata_filtered_clustered_and_frame_group_roundtrip(tmp_path):
    python(
        """import sys,runpy,json
from pathlib import Path
import anndata as ad,numpy as np,pandas as pd
from scipy.sparse import csr_matrix
m=runpy.run_path(str(Path(sys.argv[1])/'notebook_checkpoint.py'))
root=Path(sys.argv[2]); out=root/'save';out.mkdir()
data=ad.AnnData(csr_matrix(np.arange(12).reshape(6,2)),obs=pd.DataFrame({'group':['A']*6},index=[f'c{i}' for i in range(6)]),var=pd.DataFrame(index=['g1','g2']))
data.layers['counts']=data.X.copy(); data=data[[1,3,5]].copy()
data.obs['cluster']=['one','two','one']; data.obsm['X_umap']=np.arange(6).reshape(3,2)
data.uns['neighbors']={'params':{'method':'umap','n_neighbors':2}}
frame=pd.DataFrame({'gene':['g1','g2'],'p':[.1,.2]}); old=id(data);dtype=data.obs.dtypes.copy()
request={'objects':[{'logicalKey':'adata','symbol':'data','adapter':'anndata'}, {'logicalKey':'results','symbol':'frame','adapter':'python_table'}],'maxFileBytes':1000000,'maxBytes':2000000}
m['save'](globals(),request,out)
assert id(data)==old and data.obs.dtypes.equals(dtype)
saved=json.loads((out/'saved.json').read_text());assert len(saved['objects'])==2
loaded=m['verify'](out/'adata.h5ad','h5ad',[3,2]); loaded_frame=m['verify'](out/'results.pkl','pickle5',[2,2])
assert loaded.obs.index.tolist()==['c1','c3','c5'] and loaded.obs['cluster'].tolist()==['one','two','one']
np.testing.assert_array_equal(loaded.layers['counts'].toarray(),data.layers['counts'].toarray())
np.testing.assert_array_equal(loaded.obsm['X_umap'],data.obsm['X_umap']);pd.testing.assert_frame_equal(loaded_frame,frame)
""",
        tmp_path,
    )


def test_actual_failed_h5ad_write_preserves_string_dtype_and_file_limit(tmp_path):
    python(
        """import sys,runpy,resource,signal
from pathlib import Path
import anndata as ad,numpy as np,pandas as pd
m=runpy.run_path(str(Path(sys.argv[1])/'notebook_checkpoint.py'));out=Path(sys.argv[2])
data=ad.AnnData(np.ones((100,10)),obs=pd.DataFrame({'label':['a']*100},index=[f'c{i}' for i in range(100)]));old=id(data);dtype=data.obs.dtypes.copy()
before=resource.getrlimit(resource.RLIMIT_FSIZE);handler=signal.getsignal(signal.SIGXFSZ)
try:m['save'](globals(),{'objects':[{'logicalKey':'adata','symbol':'data','adapter':'anndata'}],'maxFileBytes':4096,'maxBytes':4096},out)
except (OSError,ValueError,RuntimeError):pass
else:raise AssertionError('bounded write unexpectedly succeeded')
assert id(data)==old and data.obs.dtypes.equals(dtype)
assert resource.getrlimit(resource.RLIMIT_FSIZE)==before and signal.getsignal(signal.SIGXFSZ)==handler
assert not (out/'saved.json').exists() and (out/'adata.h5ad').stat().st_size<=4096
assert data.X.sum()==1000
""",
        tmp_path,
    )


def test_actual_unsupported_objects_are_rejected_before_callback_or_write(tmp_path):
    python(
        """import sys,runpy
from pathlib import Path
import anndata as ad,numpy as np,pandas as pd
m=runpy.run_path(str(Path(sys.argv[1])/'notebook_checkpoint.py'));out=Path(sys.argv[2])
class Hostile:
 def __reduce__(self): raise AssertionError('pickle callback executed')
data=pd.DataFrame({'bad':[Hostile()]})
request={'objects':[{'logicalKey':'bad','symbol':'data','adapter':'python_table'}],'maxFileBytes':1000000,'maxBytes':1000000}
try:m['save'](globals(),request,out)
except ValueError as e:assert str(e)=='unsupported_serializer'
else:raise AssertionError('custom object accepted')
assert not list(out.iterdir())
data=ad.AnnData(np.ones((3,2))); view=data[:2]
for value in [view]:
 try:m['supported'](value,'anndata')
 except ValueError:pass
 else:raise AssertionError('view accepted')
""",
        tmp_path,
    )


@pytest.mark.parametrize("fault", ["flush"])
def test_actual_hdf5_parent_flush_failure_keeps_data_and_has_no_native_handles(
    tmp_path, fault
):
    python(
        "fault="
        + repr(fault)
        + "\n"
        + """import sys,runpy,io
from pathlib import Path
import anndata as ad,numpy as np,pandas as pd,h5py
m=runpy.run_path(str(Path(sys.argv[1])/'notebook_checkpoint.py'));root=Path(sys.argv[2])
data=ad.AnnData(np.arange(1000).reshape(100,10),obs=pd.DataFrame({'label':['a']*100},index=[f'c{i}' for i in range(100)]))
old=id(data);dtype=data.obs.dtypes.copy();values=data.X.copy();obs=data.obs.copy(deep=True)
real_open=Path.open;failures=[];closed=[]
class FaultFile(io.FileIO):
 def fail(self,method):
  if fault==method and not failures:
   failures.append(method);raise OSError('injected_'+method)
 def write(self,contents):
  if fault=='short_write' and not failures:
   failures.append(fault);return super().write(memoryview(contents).cast('B')[:-1])
  self.fail('write');return super().write(contents)
 def flush(self):self.fail('flush');return super().flush()
 def truncate(self,*args):self.fail('truncate');return super().truncate(*args)
 def close(self):
  if not self.closed:
   assert not h5py.h5f.get_obj_ids(types=h5py.h5f.OBJ_FILE),'HDF5 handle outlived its file'
   closed.append(True)
  return super().close()
def patched(path,mode='r',*args,**kwargs):
 if path.suffix=='.h5ad' and mode=='r+b':return FaultFile(path,mode)
 return real_open(path,mode,*args,**kwargs)
request={'objects':[{'logicalKey':'adata','symbol':'data','adapter':'anndata'}],'maxFileBytes':200000,'maxBytes':200000}
out=root/'failed';out.mkdir();Path.open=patched
try:
 try:m['save'](globals(),request,out)
 except (OSError,ValueError,RuntimeError):pass
 else:raise AssertionError('failed IO was published')
finally:Path.open=real_open
assert failures==[fault] and closed
assert not (out/'saved.json').exists() and sum(p.stat().st_size for p in out.iterdir())<=200000
assert id(data)==old and data.obs.dtypes.equals(dtype)
np.testing.assert_array_equal(data.X,values);pd.testing.assert_frame_equal(data.obs,obs)
assert data.X.sum()==499500 and np.dot(data.X.T,data.X).shape==(10,10)
good=root/'good';good.mkdir();m['save'](globals(),request,good)
assert (good/'saved.json').exists() and id(data)==old
""",
        tmp_path,
    )


def test_actual_repeated_hdf5_limits_leave_no_handles_or_extended_files(tmp_path):
    python(
        """import sys,runpy
from pathlib import Path
import anndata as ad,numpy as np,pandas as pd,h5py
m=runpy.run_path(str(Path(sys.argv[1])/'notebook_checkpoint.py'));root=Path(sys.argv[2])
data=ad.AnnData(np.arange(1000).reshape(100,10),obs=pd.DataFrame({'label':['a']*100},index=[f'c{i}' for i in range(100)]));old=id(data);dtype=data.obs.dtypes.copy()
limits=[1,64,512,4096,8192]*2
for index,limit in enumerate(limits):
 out=root/str(index);out.mkdir()
 try:m['save'](globals(),{'objects':[{'logicalKey':'adata','symbol':'data','adapter':'anndata'}],'maxFileBytes':limit,'maxBytes':limit},out)
 except (OSError,ValueError,RuntimeError):pass
 else:raise AssertionError('bounded write unexpectedly succeeded')
 assert not h5py.h5f.get_obj_ids(types=h5py.h5f.OBJ_FILE)
 assert not (out/'saved.json').exists() and sum(p.stat().st_size for p in out.iterdir())<=limit
 assert id(data)==old and data.obs.dtypes.equals(dtype) and data.X.sum()==499500
assert sum(p.stat().st_size for p in root.rglob('*') if p.is_file())<=sum(limits)
""",
        tmp_path,
    )


def test_r_real_rds_roundtrip_and_pipe_limit_keep_original_object(tmp_path):
    r = Path(sys.executable).with_name("Rscript")
    if not r.exists():
        pytest.skip("Explicit scientific Rscript is unavailable")
    code = r"""args<-commandArgs(trailingOnly=TRUE);root<-args[[1]];directory<-args[[2]];python<-args[[3]]
source(file.path(root,'notebook_checkpoint.R'))
suppressPackageStartupMessages(library(DESeq2))
set.seed(1); counts<-matrix(as.integer(rnbinom(1000,size=2,mu=100)),nrow=200)
rownames(counts)<-paste0('g',seq_len(nrow(counts)));colnames(counts)<-paste0('s',seq_len(ncol(counts)))
dds<-DESeqDataSetFromMatrix(counts,data.frame(group=factor(c('a','a','b','b','b')),row.names=colnames(counts)),~group)
dds<-DESeq(dds,quiet=TRUE,fitType='mean');res<-as.data.frame(results(dds))
before<-serialize(list(dds,res),NULL); output<-file.path(directory,'saved');dir.create(output)
request<-list(objects=list(list(logicalKey='dds',symbol='dds',adapter='r_rds'),list(logicalKey='res',symbol='res',adapter='r_rds')),maxFileBytes=2000000,maxBytes=4000000)
checkpoint_save(.GlobalEnv,request,output,python,file.path(root,'checkpoint_io.py'))
stopifnot(identical(serialize(list(dds,res),NULL),before))
loaded<-checkpoint_verify(file.path(output,'dds.rds'),c(200,5));loaded_res<-checkpoint_verify(file.path(output,'res.rds'),dim(res))
stopifnot(identical(counts(loaded),counts(dds)),identical(as.data.frame(colData(loaded)),as.data.frame(colData(dds))),identical(loaded_res,res))
bad<-file.path(directory,'bad');dir.create(bad);request$maxFileBytes<-100L;request$maxBytes<-100L
failed<-tryCatch({checkpoint_save(.GlobalEnv,request,bad,python,file.path(root,'checkpoint_io.py'));FALSE},error=function(e) TRUE)
stopifnot(failed,identical(serialize(list(dds,res),NULL),before),!file.exists(file.path(bad,'saved.json')))
stopifnot(all(file.info(list.files(bad,full.names=TRUE))$size<=100))
"""
    result = subprocess.run(
        [str(r), "--vanilla", "-", str(ROOT), str(tmp_path), sys.executable],
        input=code,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    manifest = json.loads((tmp_path / "saved/saved.json").read_text())
    assert [item["type"] for item in manifest["objects"]] == [
        "R.DESeqDataSet",
        "R.data.frame",
    ]


def test_bounded_sink_leaves_no_published_file_on_overflow(tmp_path):
    import io

    with pytest.raises(ValueError, match="checkpoint_byte_limit"):
        checkpoint_io.write(io.BytesIO(b"x" * 100), tmp_path / "saved.rds", 40)
    assert not (tmp_path / "saved.rds").exists()
    assert (tmp_path / "saved.rds.partial").stat().st_size <= 40
