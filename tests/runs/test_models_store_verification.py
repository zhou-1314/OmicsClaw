from omicsclaw.runs import Run,Job,Artifact,Verification,transition
from omicsclaw.runs.store import RunStore
from omicsclaw.runs.verification import verify_file,verify_json
import tempfile,json,hashlib

def test_states():
 assert transition("created","created")=="created"; assert transition("created","running")=="running"
 try: transition("succeeded","running"); assert False
 except ValueError: pass

def test_store_roundtrip():
 s=RunStore(); r=Run(session_id="s"); s.save(r); s.save(Job(run_id=r.run_id)); assert s.load(r.run_id).session_id=="s"; assert len(s.list())==2

def test_verification(tmp_path):
 p=tmp_path/"a"; p.write_text("x"); c=hashlib.sha256(b"x").hexdigest(); assert verify_file(p,c).verdict=="pass"; assert verify_file(p,"bad").verdict=="fail"; assert verify_file(tmp_path/"no").verdict=="fail"
 j=tmp_path/"j"; j.write_text(json.dumps({"x":1})); assert verify_json(j,["x"]).verdict=="pass"; assert verify_json(j,["y"]).verdict=="warn"
