"""D0–D3 integrity, durable consumption, and offline-only execution utilities."""
import csv
import importlib.metadata
import json
import os
import socket
import subprocess
from contextlib import contextmanager, ExitStack
from pathlib import Path
from unittest.mock import patch

import numpy as np

from tools.audit_adaptive_budget import sha
from tools.offline_accuracy import write_json, confusion, macro, ROOT

DEVELOPMENT=tuple(f'{family}-{v}' for family in
    ('smooth_roads','far_residual','detours','pocket_channel','endpoint_gaps') for v in (0,1))
OLD_PROTOCOL='54b42676998f423214e619ed01dfdc3180ce4896d47f36e4b71050fc11a85fc3'
CAPS={'D1':24000,'D2':60,'D3':4800,'intervention':128}


class DiagnosticStop(BaseException):
    """Never convert an integrity failure to an unknown provider observation."""


def require_development(identity):
    if identity not in DEVELOPMENT:
        raise DiagnosticStop('Layout is outside the ten-layout development allowlist')
    return identity


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


@contextmanager
def no_network():
    def blocked(*args,**kwargs):
        raise DiagnosticStop('Network blocked by diagnostic harness')
    with ExitStack() as stack:
        for target in ('socket.socket.connect','socket.socket.connect_ex','socket.create_connection',
                       'httpx.Client.send','httpx.AsyncClient.send'):
            stack.enter_context(patch(target,blocked))
        yield


class RunLedger:
    def __init__(self,directory,data):
        self.directory,self.data,self.broken=directory,data,False

    @classmethod
    def create(cls,directory,caps=None):
        directory.mkdir(parents=True,exist_ok=False)
        with (directory/'started.marker').open('x',encoding='ascii') as f:
            f.write('No reset or repeat after interruption');f.flush();os.fsync(f.fileno())
        ledger=cls(directory,dict(caps=caps or CAPS,used={p:0 for p in (caps or CAPS)},runs={},live_requests=0))
        ledger.save()
        return ledger

    @classmethod
    def open(cls,directory):
        return cls(directory,read_json(directory/'ledger.json'))

    def save(self):
        if self.broken:
            raise DiagnosticStop('Ledger is already failed')
        try:
            write_json(self.directory/'ledger.json',self.data)
        except Exception:
            self.broken=True
            raise DiagnosticStop('Ledger write failed; stop without refund') from None

    def start(self,phase,identity,limit):
        if self.broken or identity in self.data['runs'] or any(r['status']=='running' for r in self.data['runs'].values()):
            raise DiagnosticStop('Existing or interrupted run cannot be restarted')
        if phase not in self.data['caps'] or limit<0 or limit>self.data['caps'][phase]:
            raise DiagnosticStop('Invalid phase limit')
        self.data['runs'][identity]=dict(phase=phase,limit=limit,used=0,status='running')
        self.save()

    def consume(self,phase,identity,event):
        row=self.data['runs'][identity]
        if self.broken or row['status']!='running' or row['phase']!=phase:
            raise DiagnosticStop('Run closed or ledger failed')
        if row['used']>=row['limit'] or self.data['used'][phase]>=self.data['caps'][phase]:
            raise DiagnosticStop('Hard diagnostic budget reached')
        row['used']+=1;self.data['used'][phase]+=1
        self.save()  # Conservative reservation is durable BEFORE the attempt is consumed.
        try:
            with (self.directory/f'{identity}-consumption.jsonl').open('a',encoding='utf-8') as f:
                f.write(json.dumps(dict(ordinal=row['used'],phase=phase,**event),allow_nan=False)+'\n')
                f.flush();os.fsync(f.fileno())
        except Exception:
            self.broken=True
            raise DiagnosticStop('Attempt record failed; no subsequent consumption') from None

    def finish(self,identity,status,**metadata):
        row=self.data['runs'][identity]
        if row['status']!='running':
            raise DiagnosticStop('Terminal run cannot be rewritten')
        row.update(status=status,**metadata)
        self.save()


def write_csv(path,rows):
    if not rows:
        return
    keys=list(dict.fromkeys(k for r in rows for k in r))
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,keys);writer.writeheader()
        writer.writerows({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v for k,v in r.items()} for r in rows)


def runtime_manifest():
    import life_circle.engine as engine
    source=Path(engine.__file__).resolve().parent
    return dict(git_head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        algorithm_source=str(source), algorithm_sha256={p.name:sha(p) for p in sorted(source.glob('*.py'))},
        tool_sha256={p.name:sha(p) for p in sorted((ROOT/'backend/tools').glob('*.py'))},
        packages={p:importlib.metadata.version(p) for p in ('numpy','shapely','contourpy','httpx','pytest')})


def audit_old(old,snapshot):
    """Hash sealed files, but only parse development data. Never regenerate labels."""
    protocol=read_json(old/'protocol.json')
    if sha(old/'protocol.json')!=OLD_PROTOCOL or (old/'protocol.sha256').read_text()!=OLD_PROTOCOL:
        raise DiagnosticStop('Frozen protocol mismatch')
    inputs={str(old/'protocol.json'):OLD_PROTOCOL}
    for name,digest in protocol['source_sha256'].items():
        if sha(snapshot/name)!=digest:
            raise DiagnosticStop('Frozen source mismatch')
        inputs[str(snapshot/name)]=digest
    import life_circle.engine as engine
    for path in Path(engine.__file__).parent.glob('*.py'):
        if sha(path)!=protocol['source_sha256'][f'life-circle-algorithm/src/life_circle/{path.name}']:
            raise DiagnosticStop('Loaded algorithm differs from frozen source')
    for name,digest in protocol['independent_hashes'].items():
        if sha(old/name)!=digest:
            raise DiagnosticStop('Independent file hash mismatch')
        inputs[str(old/name)]=digest
    manifest=read_json(old.parent/'freeze-manifest.json')
    # Verify original outputs against the historical manifest, including metrics and samples.
    artifacts=manifest['artifacts']
    for name,digest in artifacts.items():
        path=Path(name) if Path(name).is_absolute() else old.parent/name
        if sha(path)!=digest:
            raise DiagnosticStop('Historical artifact mismatch')
        inputs[str(path)]=digest
    rows=read_json(old/'development/metrics.json')
    csv_rows=list(csv.DictReader((old/'development/metrics.csv').open(encoding='utf-8-sig',newline='')))
    if len(rows)!=40 or len(csv_rows)!=40:
        raise DiagnosticStop('Expected forty original rows')
    for row,csv_row in zip(rows,csv_rows):
        identity=require_development(row['layout'])
        for key,value in row.items():
            if isinstance(value,(dict,list,int,float)):
                other=json.loads(csv_row[key])
            elif value is None:
                other=None if csv_row[key]=='' else csv_row[key]
            else:
                other=csv_row[key]
            if other!=value:
                raise DiagnosticStop('JSON/CSV mismatch')
        stem=f"{identity}-{row['budget']}-{row['method']}"
        with np.load(old/f'{identity}-evaluation.npz') as ref, np.load(old/f'development/{stem}-predictions.npz') as pred:
            measured=confusion(ref['truth']<=900,pred['prediction'])
        if any(row[k]!=v for k,v in measured.items()):
            raise DiagnosticStop('Frozen prediction/count mismatch')
        if len(read_json(old/f'development/{stem}-samples.json'))!=row['actual_requests']:
            raise DiagnosticStop('Frozen sample/count mismatch')
    summary=read_json(old/'development/summary.json')
    for method in ('baseline','C1'):
        actual=macro([r for r in rows if r['method']==method and r['budget']==800])
        if actual!=summary[method]:
            raise DiagnosticStop('Macro mismatch')
    base=[r for r in rows if r['method']=='baseline' and r['budget']==800]
    return dict(passed=True,protocol_sha256=OLD_PROTOCOL,inputs=inputs,development=list(DEVELOPMENT),
        sealed_hash_only=10,old_sealed_predictions_exist=(old/'sealed').exists(),runs=40,
        recorded_attempts=sum(r['actual_requests'] for r in rows),
        FN_unknown_macro_pp=float(np.mean([r['FN_unknown']/r['positives'] for r in base])*100),
        FN_known_macro_pp=float(np.mean([r['FN_known']/r['positives'] for r in base])*100),
        original_gate=summary['gate'],runtime=runtime_manifest())


def verify_inputs(audit):
    if any(sha(Path(p))!=digest for p,digest in audit['inputs'].items()):
        raise DiagnosticStop('Frozen input changed during diagnostics')
