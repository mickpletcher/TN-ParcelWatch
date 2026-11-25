#!/usr/bin/env python3
"""One bounded, sanitized status line for the Monday supervisor."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid
from supervisor import assess


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',required=True,type=Path)
    p.add_argument('--output',required=True,type=Path)
    p.add_argument('--state',required=True,type=Path)
    p.add_argument('--fixtures',type=Path)
    p.add_argument('--send',action='store_true')
    a=p.parse_args(); os.umask(0o077)
    run_id=str(uuid.uuid4())
    verdict='FAILED'; rc=1; complete=False; delivery='unknown'; counts={}; queued=0; attempted=0
    try:
        a.output.mkdir(parents=True,exist_ok=True,mode=0o700)
        # Every invocation has its own output: a stale summary cannot count as success.
        out=Path(tempfile.mkdtemp(prefix='run-',dir=a.output))
        cmd=[sys.executable,str(Path(__file__).with_name('watch.py')),'--run-id',run_id,'--config',str(a.config),'--output',str(out),'--state',str(a.state)]
        if a.fixtures: cmd+=['--fixtures',str(a.fixtures)]
        cmd+=['--send' if a.send else '--dry-run']
        # Reports are on disk; discard raw subprocess output even on failure.
        result=subprocess.run(cmd,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=900)
        rc=result.returncode
        data=json.loads((out/'summary.json').read_text())
        if data.get('run_id') != run_id: raise ValueError()
        verdict=assess(rc,data,a.send)
        if verdict!='FAILED':
            counts=data['counts']; complete=data['report_complete']; delivery=data['delivery']; queued=data['repair_events_created']; attempted=data['source_lookups_attempted']
    except (OSError,ValueError,KeyError,subprocess.TimeoutExpired): pass
    owners={'expected':6,'verified':counts.get('MATCH',0)+counts.get('OWNER_CHANGE',0),'changed':counts.get('OWNER_CHANGE',0)}
    taxes={'expected':6,'verified':counts.get('ZERO',0)+counts.get('BALANCE_NONZERO',0),'nonzero':counts.get('BALANCE_NONZERO',0)}
    line={'v':1,'run_id':run_id,'verdict':verdict,'process_exit':rc,'report_complete':complete,'lookups_attempted':attempted,'owners':owners,'taxes':taxes,'delivery':delivery,'new_repairs':queued,'resend':False}
    print(json.dumps(line,separators=(',',':')))
    return 0 if verdict in ('SUCCESS','SUCCESS_WITH_FINDINGS') else 1

if __name__=='__main__': raise SystemExit(main())
