#!/usr/bin/env python3
"""Local-only handoff proof. No production endpoint or model client is implemented."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import urllib.parse
import urllib.request
from watch import ADAPTERS,CODES,NoRedirect,private_write,lock


def validate(event):
    if set(event) != {'schema_version','project','adapter','failure_code','parser_version','event_id'}: raise ValueError()
    if event['schema_version']!=1 or event['project']!='stewart-property-watch': raise ValueError()
    if event['adapter'] not in ADAPTERS or event['failure_code'] not in CODES: raise ValueError()
    if event['parser_version']!='1.0.0': raise ValueError()
    data={k:v for k,v in event.items() if k!='event_id'}
    if hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()!=event['event_id']: raise ValueError()
    return event


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state',required=True,type=Path)
    p.add_argument('--endpoint',required=True)
    a=p.parse_args(); u=urllib.parse.urlsplit(a.endpoint)
    if u.scheme!='http' or u.hostname not in ('127.0.0.1','::1') or u.username or u.password or u.query or u.fragment:
        p.error('Only an explicit loopback stub endpoint is supported')
    os.umask(0o077); sent=0
    with lock(a.state/'relay.lock'):
        for f in sorted((a.state/'repair-outbox').glob('*.json')):
            e=validate(json.loads(f.read_text())); ledger=a.state/'repair-delivery'/(e['event_id']+'.json')
            if ledger.exists(): continue
            private_write(ledger,json.dumps({'status':'pending_or_uncertain'}))
            req=urllib.request.Request(a.endpoint,data=json.dumps(e).encode(),headers={'Content-Type':'application/json','Idempotency-Key':e['event_id']},method='POST')
            with urllib.request.build_opener(NoRedirect()).open(req,timeout=5) as response:
                if response.status!=202: raise ValueError('Stub did not accept event')
            private_write(ledger,json.dumps({'status':'accepted'}));sent+=1
    print(json.dumps({'schema_version':1,'stub_events_accepted':sent}))

if __name__=='__main__': main()
