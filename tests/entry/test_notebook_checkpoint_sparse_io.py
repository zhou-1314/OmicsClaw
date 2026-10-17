"""Native HDF5 failures must leave the original sparse/raw object usable."""

import asyncio

import pytest

from tests.entry.test_notebook_checkpoint_serializers import python

DATA = """
import sys,runpy,io
from pathlib import Path
import anndata as ad,numpy as np,pandas as pd,h5py
from scipy import sparse
m=runpy.run_path(str(Path(sys.argv[1])/'notebook_checkpoint.py'));root=Path(sys.argv[2])
value=ad.AnnData(sparse.csr_matrix(np.log1p(np.arange(384).reshape(32,12)+1)),obs=pd.DataFrame({'sample':['A']*16+['B']*16},index=[f'c{i}' for i in range(32)]),var=pd.DataFrame(index=[f'g{i}' for i in range(12)]))
value.layers['counts']=sparse.csr_matrix(np.arange(384).reshape(32,12)+1)
value.layers['lognorm']=value.X.copy();value.obsm['X_umap']=np.arange(64).reshape(32,2)
value.raw=ad.AnnData(np.full((32,12),999.),obs=value.obs.copy(),var=value.var.copy())
original=id(value);obs=value.obs.copy(deep=True);counts=value.layers['counts'].copy();raw=value.raw.X.copy()
def check():
 assert id(value)==original
 pd.testing.assert_frame_equal(value.obs,obs)
 np.testing.assert_array_equal(value.layers['counts'].toarray(),counts.toarray())
 np.testing.assert_array_equal(value.raw.X,raw)
 assert not h5py.h5f.get_obj_ids(types=h5py.h5f.OBJ_FILE)
 assert (value.X.T@value.X).shape==(12,12)
def save(directory,limit):
 m['save'](globals(),{'objects':[{'logicalKey':'adata','symbol':'value','adapter':'anndata'}],'maxFileBytes':limit,'maxBytes':limit},directory)
"""


@pytest.mark.parametrize("matrix", ["csr", "csc"])
def test_sparse_raw_limits_keep_native_handles_data_and_physical_bytes(
    tmp_path, matrix
):
    python(
        DATA
        + ("value.X=value.X.tocsc()\n" if matrix == "csc" else "")
        + """
limits=[1,64,256,512,1024,4096,8192,16384,32768]*2
for index,limit in enumerate(limits):
 directory=root/str(index);directory.mkdir()
 try:save(directory,limit)
 except (OSError,ValueError,RuntimeError) as error:
  assert str(error) in ('checkpoint_byte_limit','checkpoint_native_write_failed'),repr(error)
 else:raise AssertionError('quota did not reject the complete file')
 check()
 assert not (directory/'saved.json').exists()
 assert sum(p.stat().st_size for p in directory.iterdir())<=limit
good=root/'good';good.mkdir();save(good,1000000);check()
loaded=m['verify'](good/'adata.h5ad','h5ad',[32,12])
np.testing.assert_array_equal(loaded.layers['counts'].toarray(),counts.toarray())
np.testing.assert_array_equal(loaded.raw.X,raw)
""",
        tmp_path,
    )


@pytest.mark.parametrize(
    "fault", ["native_write", "native_close", "native_exit", "native_segv"]
)
def test_native_child_failures_keep_parent_object_and_allow_later_save(tmp_path, fault):
    python(
        "fault="
        + repr(fault)
        + "\n"
        + DATA
        + """
import ctypes,os
import anndata.io
real_write=anndata.io.write_elem;real_file=h5py.File
parent=os.getpid();before_children=Path('/proc/self/task/'+str(parent)+'/children').read_text()
def damaged(*args,**kwargs):
 real_write(*args,**kwargs)
 if fault=='native_write':raise OSError('injected_native_write')
 if fault=='native_exit':os._exit(73)
 if fault=='native_segv':ctypes.string_at(0)
class FailedClose:
 def __init__(self,*args,**kwargs):self.handle=real_file(*args,**kwargs)
 def __enter__(self):return self.handle
 def __exit__(self,*args):
  self.handle.close();raise OSError('injected_native_close')
anndata.io.write_elem=damaged
if fault=='native_close':h5py.File=FailedClose
directory=root/'bad';directory.mkdir()
try:
 try:save(directory,1000000)
 except ValueError as error:assert str(error)=='checkpoint_native_write_failed',repr(error)
 else:raise AssertionError('failed native child published a checkpoint')
finally:anndata.io.write_elem=real_write;h5py.File=real_file
assert os.getpid()==parent
assert Path('/proc/self/task/'+str(parent)+'/children').read_text()==before_children
assert not (directory/'saved.json').exists()
assert sum(p.stat().st_size for p in directory.iterdir())<=1000000
check();good=root/'good';good.mkdir();save(good,1000000);check()
""",
        tmp_path,
    )


@pytest.mark.usefixtures("notebook_control_rss")
def test_real_owner_sparse_save_failure_then_continue_keeps_same_kernel_object(
    tmp_path,
):
    from omicsclaw.entry.notebook.checkpoint_store import reference
    from tests.entry.test_notebook_analysis_owner import registration, settled
    from tests.entry.test_notebook_checkpoint_owner import save_command, terminal
    from tests.entry.test_notebook_r import bind, command, create, environment

    async def run():
        owner = await create(tmp_path, real=True)
        try:
            env = await environment(owner, "python", real=True)
            kernel = (await bind(owner, "python", env))["kernel"]
            source = """import anndata as ad,numpy as np,pandas as pd,os,threading
assert len(threading.enumerate())>=3
from scipy import sparse
data=ad.AnnData(sparse.csr_matrix(np.arange(384).reshape(32,12)+1),obs=pd.DataFrame({'sample':['s']*32,'barcode':[f'c{i}' for i in range(32)]},index=[f'c{i}' for i in range(32)]),var=pd.DataFrame(index=[f'g{i}' for i in range(12)]))
data.layers['counts']=data.X.copy();data.obsm['X_umap']=np.arange(64).reshape(32,2)
data.raw=ad.AnnData(np.full((32,12),999.),obs=data.obs.copy(),var=data.var.copy())
saved_identity=id(data);saved_pid=os.getpid();saved_obs=data.obs.copy(deep=True)
"""
            receipt = await owner.admit({"command": command(kernel, "create", source)})
            assert (await settled(owner, receipt["execution_id"]))[
                "status"
            ] == "completed"
            result = await owner.analysis_command(
                {"notebookId": "nb", "command": registration(kernel)}
            )
            assert (await settled(owner, result["receipt"]["result"]["operationId"]))[
                "status"
            ] == "completed"
            birth = owner.db.run(
                lambda c: c.execute(
                    "SELECT process_json FROM notebook_runtime_kernels WHERE instance_id=?",
                    (kernel["kernelInstanceId"],),
                ).fetchone()[0]
            )
            maximum = owner.checkpoints.limits["maxFileBytes"]
            owner.checkpoints.limits["maxFileBytes"] = 4096
            result = await owner.analysis_command(
                {"notebookId": "nb", "command": save_command(owner, kernel)}
            )
            op = await terminal(owner, result["receipt"]["result"]["operationId"])
            assert op["state"] == "failed" and op["stopConfirmed"], op
            failure = owner.analysis.store.state_response(
                {
                    "notebookId": "nb",
                    "subject": {
                        "kind": "checkpointFailure",
                        "id": op["failureRef"]["failureId"],
                    },
                }
            )["item"]
            decision = {
                "schemaVersion": 1,
                "commandId": "continue",
                "action": "checkpoint.continue",
                "arguments": {
                    "failure": reference(failure, "failure"),
                    "acknowledgement": "continue_without_this_checkpoint",
                },
                "expected": {
                    "analysisRevision": owner.analysis.store.state_response(
                        {"notebookId": "nb"}
                    )["analysisRevision"],
                    "kernels": [failure["source"]],
                    "objects": failure["objects"],
                    "selections": [],
                },
            }
            await owner.analysis_command({"notebookId": "nb", "command": decision})
            follow = command(
                kernel,
                "check",
                "assert os.getpid()==saved_pid and id(data)==saved_identity\npd.testing.assert_frame_equal(data.obs,saved_obs)\nassert data.layers['counts'].sum()==73920 and np.all(data.raw.X==999.)\nassert (data.X.T@data.X).shape==(12,12)",
            )
            follow.update(
                version=3,
                preconditions={
                    "analysis": owner.db.run(
                        lambda c: owner.analysis.store.context(c, "nb")
                    )["guard"]
                },
            )
            accepted = await owner.admit({"command": follow})
            final = await settled(owner, accepted["execution_id"])
            assert final["status"] == "completed", final
            assert (
                owner.db.run(
                    lambda c: c.execute(
                        "SELECT process_json FROM notebook_runtime_kernels WHERE instance_id=?",
                        (kernel["kernelInstanceId"],),
                    ).fetchone()[0]
                )
                == birth
            )
            owner.checkpoints.limits["maxFileBytes"] = maximum
            result = await owner.analysis_command(
                {"notebookId": "nb", "command": save_command(owner, kernel, "good")}
            )
            assert (await terminal(owner, result["receipt"]["result"]["operationId"]))[
                "state"
            ] == "ready"
        finally:
            await owner.close()

    asyncio.run(run())


@pytest.mark.parametrize("fault", ["budget", "allocation"])
def test_native_memory_limits_keep_parent_and_its_limits(tmp_path, fault):
    python(
        "fault="
        + repr(fault)
        + "\n"
        + DATA
        + """
import os,resource,anndata.io
before_limits={key:resource.getrlimit(key) for key in (resource.RLIMIT_AS,resource.RLIMIT_FSIZE)}
real=anndata.io.write_elem
if fault=='allocation':
 def allocate(*args,**kwargs):
  value.obs['sample']='changed only in the child'
  scratch=bytearray(256*1024*1024)
  real(*args,**kwargs)
 anndata.io.write_elem=allocate
out=root/'bad';out.mkdir()
request={'objects':[{'logicalKey':'adata','symbol':'value','adapter':'anndata'}],'maxFileBytes':1000000,'maxBytes':1000000,'maxMemoryBytes':1 if fault=='budget' else 32*1024**3}
try:
 try:m['save'](globals(),request,out)
 except ValueError as error:assert str(error)=='checkpoint_memory_limit',repr(error)
 else:raise AssertionError('memory budget was not enforced')
finally:anndata.io.write_elem=real
assert not (out/'saved.json').exists()
assert {key:resource.getrlimit(key) for key in before_limits}==before_limits
assert not Path('/proc/self/task/'+str(os.getpid())+'/children').read_text().strip()
check();good=root/'good';good.mkdir();save(good,1000000);check()
""",
        tmp_path,
    )


def test_native_writer_cancellation_reaps_child_before_preserving_parent(tmp_path):
    python(
        DATA
        + """
import os,signal,threading,time,anndata.io,json
real=anndata.io.write_elem;real_waitid=os.waitid
def repeated_wait(*args):
 result=real_waitid(*args)
 if os.getpid()==parent and result is not None:
  time.sleep(.2)
 return result
os.waitid=repeated_wait
marker=root/'started';parent=os.getpid();parent_birth=m['native_birth'](parent)
observed=[];signals=[];errors=[]
def wait_writer(*args,**kwargs):
 marker.write_text(json.dumps({'pid':os.getpid(),'birth':m['native_birth'](os.getpid())}))
 while True:time.sleep(.1)
def interrupt():
 try:
  deadline=time.monotonic()+10
  while not marker.exists():
   assert time.monotonic()<deadline
   time.sleep(.005)
  child=json.loads(marker.read_text());observed.append(child)
  assert m['native_birth'](parent)==parent_birth
  for _ in range(5):
   signals.append(True);os.kill(parent,signal.SIGINT);time.sleep(.02)
 except BaseException as error:errors.append(repr(error))
anndata.io.write_elem=wait_writer
thread=threading.Thread(target=interrupt);thread.start()
out=root/'cancelled';out.mkdir()
try:
 try:save(out,1000000)
 except KeyboardInterrupt:pass
 else:raise AssertionError('interrupt did not reach the protected wait')
finally:anndata.io.write_elem=real;os.waitid=real_waitid;thread.join(timeout=15)
assert not thread.is_alive() and not errors and len(signals)==5
assert len(observed)==1
assert not Path('/proc/'+str(observed[0]['pid'])).exists()
assert not Path('/proc/self/task/'+str(parent)+'/children').read_text().strip()
assert os.getpid()==parent and m['native_birth'](parent)==parent_birth
check();good=root/'good';good.mkdir();save(good,1000000);check()
""",
        tmp_path,
    )


def test_platform_without_native_boundary_refuses_before_fork(tmp_path):
    python(
        DATA
        + """
original_platform=sys.platform;sys.platform='darwin'
out=root/'unsupported';out.mkdir()
try:
 try:save(out,1000000)
 except ValueError as error:assert str(error)=='checkpoint_native_bounded_writer_unavailable'
 else:raise AssertionError('unsupported platform used an unsafe fallback')
finally:sys.platform=original_platform
check();assert not (out/'saved.json').exists()
good=root/'good';good.mkdir();save(good,1000000);check()
""",
        tmp_path,
    )


@pytest.fixture(scope="module")
def native_io_fault(tmp_path_factory):
    import shutil
    import subprocess
    from pathlib import Path

    compiler = shutil.which("cc")
    if compiler is None:
        pytest.skip("Native HDF5 syscall fault injection requires a C compiler")
    destination = tmp_path_factory.mktemp("checkpoint-native-fault") / "fault.so"
    source = Path(__file__).parents[1] / "fixtures/notebook_checkpoint_io_fault.c"
    subprocess.run(
        [compiler, "-shared", "-fPIC", "-o", str(destination), str(source), "-ldl"],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return destination


@pytest.mark.parametrize("fault", ["write", "short_write", "zero_write"])
def test_actual_native_syscall_failures_are_isolated(
    tmp_path, monkeypatch, native_io_fault, fault
):
    monkeypatch.setenv("LD_PRELOAD", str(native_io_fault))
    monkeypatch.setenv("OMICSCLAW_TEST_NATIVE_FAULT", fault)
    marker = tmp_path / "actual-fault.txt"
    monkeypatch.setenv("OMICSCLAW_TEST_NATIVE_MARKER", str(marker))
    python(
        "fault="
        + repr(fault)
        + "\n"
        + DATA
        + """
import os
out=root/'faulted-native';out.mkdir()
try:m['save'](globals(),{'objects':[{'logicalKey':'adata','symbol':'value','adapter':'anndata'}],'maxFileBytes':1000000,'maxBytes':1000000,'timeoutSeconds':.3},out)
except ValueError as error:assert str(error)==('checkpoint_writer_timeout' if fault=='zero_write' else 'checkpoint_native_write_failed'),repr(error)
else:raise AssertionError('native IO failure was published')
assert not (out/'saved.json').exists() and 4096<=sum(p.stat().st_size for p in out.iterdir())<=1000000
assert not Path('/proc/self/task/'+str(os.getpid())+'/children').read_text().strip()
check();good=root/'good';good.mkdir();save(good,1000000);check()
""",
        tmp_path,
    )
    assert fault in marker.read_text().splitlines()


def test_native_writer_dies_when_its_scientific_parent_dies(tmp_path):
    import json
    import subprocess
    import sys
    import time
    from pathlib import Path

    import psutil

    from tests.entry.test_notebook_checkpoint_serializers import ROOT

    script = (
        DATA
        + """
import os,time,json,anndata.io
marker=root/'child.json'
def blocked(*args,**kwargs):
 temporary=marker.with_suffix('.partial')
 temporary.write_text(json.dumps({'pid':os.getpid(),'birth':m['native_birth'](os.getpid())}))
 temporary.replace(marker)
 while True:time.sleep(.1)
anndata.io.write_elem=blocked
out=root/'output';out.mkdir();save(out,1000000)
"""
    )
    parent = subprocess.Popen(
        [sys.executable, "-I", "-c", script, str(ROOT), str(tmp_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    birth = psutil.Process(parent.pid).create_time()
    child = None
    try:
        deadline = time.monotonic() + 15
        while not (tmp_path / "child.json").exists():
            assert parent.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        child = json.loads((tmp_path / "child.json").read_text())
        assert psutil.Process(parent.pid).create_time() == birth
        parent.kill()
        parent.wait(timeout=5)
        while time.monotonic() < deadline:
            try:
                if psutil.Process(child["pid"]).status() == psutil.STATUS_ZOMBIE:
                    break
            except psutil.NoSuchProcess:
                break
            time.sleep(0.01)
        else:
            pytest.fail("serializer child survived its scientific parent")
        assert not (tmp_path / "output/saved.json").exists()
    finally:
        if parent.poll() is None:
            assert psutil.Process(parent.pid).create_time() == birth
            parent.kill()
            parent.wait(timeout=5)
        if child:
            try:
                stat = (
                    Path("/proc/" + str(child["pid"]) + "/stat")
                    .read_text()
                    .rsplit(")", 1)[1]
                    .split()
                )
                if stat[19] == child["birth"] and stat[0] != "Z":
                    psutil.Process(child["pid"]).kill()
            except (OSError, psutil.NoSuchProcess):
                pass
        parent.stderr.close()


def test_native_memory_checks_current_cgroup_and_visible_ancestors(
    tmp_path, monkeypatch
):
    from pathlib import Path

    from omicsclaw.skillenv.notebook_checkpoint import native_cgroup_headroom

    root = tmp_path / "memory"
    current = root / "users/session"
    current.mkdir(parents=True)
    (root / "memory.max").write_text("1000")
    (root / "memory.current").write_text("200")
    (root / "users/memory.max").write_text("70")
    (root / "users/memory.current").write_text("40")
    (current / "memory.max").write_text("200")
    (current / "memory.current").write_text("50")
    real = Path.read_text

    def read(path, *args, **kwargs):
        if str(path) == "/proc/self/cgroup":
            return "0::/users/session\n"
        if str(path) == "/proc/self/mountinfo":
            return f"1 2 0:3 / {root} ro - cgroup2 cgroup rw\n"
        return real(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    assert native_cgroup_headroom() == 30
    (current / "memory.current").write_text("199")
    assert native_cgroup_headroom() == 1
