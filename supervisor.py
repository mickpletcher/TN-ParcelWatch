#!/usr/bin/env python3
"""Validate the compact report contract without reading personal reports or logs."""
import argparse
import json
import uuid
from pathlib import Path


def assess(exit_code, s, require_email=False):
    try:
        uuid.UUID(s['run_id'])
        if s['schema_version'] != 1 or s['program'] != 'stewart-property-watch': return 'FAILED'
        if s['report_complete'] is not True or exit_code not in (0,1): return 'FAILED'
        if (s['expected_checks'],s['completed_checks'],s['owner_checks'],s['tax_checks']) != (12,12,6,6): return 'FAILED'
        c=s['counts']
        if set(c)!={'MATCH','OWNER_CHANGE','ZERO','BALANCE_NONZERO','UNVERIFIABLE'}: return 'FAILED'
        if any(type(v) is not int or v<0 for v in c.values()) or sum(c.values())!=12: return 'FAILED'
        if c['MATCH']+c['OWNER_CHANGE']>6 or c['ZERO']+c['BALANCE_NONZERO']>6: return 'FAILED'
        if s['ownership'] != {'expected':6,'verified':c['MATCH']+c['OWNER_CHANGE']}: return 'FAILED'
        if s['taxes'] != {'expected':6,'verified':c['ZERO']+c['BALANCE_NONZERO']}: return 'FAILED'
        delivery=s['delivery']
        if delivery not in ('not_requested','smtp_accepted'): return 'DELIVERY_UNVERIFIED'
        if require_email and delivery!='smtp_accepted': return 'DELIVERY_UNVERIFIED'
        if c['UNVERIFIABLE']:
            return 'PARTIAL' if exit_code==1 and s['run_status']=='PARTIAL' else 'FAILED'
        if exit_code!=0: return 'FAILED'
        if c['OWNER_CHANGE'] or c['BALANCE_NONZERO']:
            return 'SUCCESS_WITH_FINDINGS' if s['run_status']=='ALERT' else 'FAILED'
        return 'SUCCESS' if s['run_status']=='ALL_CLEAR' else 'FAILED'
    except (KeyError,TypeError,ValueError): return 'FAILED'


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--summary',required=True,type=Path)
    p.add_argument('--exit-code',required=True,type=int)
    p.add_argument('--require-email',action='store_true')
    a=p.parse_args()
    try: verdict=assess(a.exit_code,json.loads(a.summary.read_text()),a.require_email)
    except (OSError,ValueError): verdict='FAILED'
    print(json.dumps({'schema_version':1,'verdict':verdict,'automatic_resend':False}))
    return 0 if verdict in ('SUCCESS','SUCCESS_WITH_FINDINGS') else 1

if __name__=='__main__': raise SystemExit(main())
