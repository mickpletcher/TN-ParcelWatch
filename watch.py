#!/usr/bin/env python3
"""Deterministic property watch. Python 3.11+, standard library only."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from email.utils import format_datetime
import hashlib
from html import escape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import smtplib
import socket
import ssl
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

VERSION = '1.0.0'
OWNER_URL = 'https://tnmap.tn.gov/arcgis/rest/services/CADASTRAL/STATEWIDE_PARCELS_WEB_MERCATOR/MapServer/0/query'
CODES = frozenset({'unconfigured','empty','ambiguous','identity_mismatch','schema','invalid_money','stale','access_denied','http_error','network','oversize','redirect','content_type'})
ADAPTERS = frozenset({'tnmap_owner','tax_table'})
MAX_BYTES = 2_000_000

class Failure(Exception):
    def __init__(self, code):
        assert code in CODES
        self.code = code


def private_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    path.chmod(0o600)


@contextmanager
def lock(path):
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Failure('redirect')


def fetch(url, expected_type, opener=None, sleep=time.sleep):
    """GET only; no login, tokens, redirects, challenge solving or raw error logging."""
    opener = opener or urllib.request.build_opener(NoRedirect())
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'StewartPropertyWatch/1.0', 'Accept': expected_type, 'Cache-Control': 'no-cache'})
            with opener.open(req, timeout=20) as r:
                if r.status != 200:
                    raise Failure('http_error')
                typ = r.headers.get_content_type()
                if typ != expected_type:
                    raise Failure('content_type')
                raw = r.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise Failure('oversize')
                return raw.decode('utf-8-sig')
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 407, 429):
                raise Failure('access_denied') from None
            if e.code not in (500, 502, 503, 504) or attempt == 2:
                raise Failure('http_error') from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError):
            if attempt == 2:
                raise Failure('network') from None
        except UnicodeError:
            raise Failure('schema') from None
        sleep(2 ** attempt)


def parse_owner(raw, record):
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        raise Failure('schema') from None
    if not isinstance(data, dict):
        raise Failure('schema')
    if 'error' in data:
        err = data['error']
        raise Failure('access_denied' if isinstance(err, dict) and err.get('code') in (401,403,498,499) else 'schema')
    if data.get('exceededTransferLimit'):
        raise Failure('ambiguous')
    rows = data.get('features')
    if not isinstance(rows, list):
        raise Failure('schema')
    if not rows:
        raise Failure('empty')
    if len(rows) != 1:
        raise Failure('ambiguous')
    if not isinstance(rows[0], dict) or not isinstance(rows[0].get('attributes'), dict):
        raise Failure('schema')
    a = rows[0]['attributes']
    if not all(isinstance(a.get(k), str) and a[k].strip() for k in ('GISLINK','OWNER')):
        raise Failure('schema')
    if a['GISLINK'] != record['parcel']:
        raise Failure('identity_mismatch')
    # Exact primary owner comparison: no case folding, punctuation removal or fuzzy matching.
    return {'observed': a['OWNER'], 'status': 'MATCH' if a['OWNER'] == record['expected'] else 'OWNER_CHANGE'}


class Tables(HTMLParser):
    """Collect table cells; reject nested tables and malformed boundaries."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []; self.table = None; self.row = None; self.cell = None
    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            if self.table is not None: raise Failure('schema')
            self.table = []
        elif tag == 'tr' and self.table is not None:
            if self.row is not None: raise Failure('schema')
            self.row = []
        elif tag in ('td','th') and self.row is not None:
            if self.cell is not None: raise Failure('schema')
            if any(k in ('rowspan','colspan') and v != '1' for k,v in attrs): raise Failure('schema')
            self.cell = []
    def handle_data(self, data):
        if self.cell is not None: self.cell.append(data)
    def handle_endtag(self, tag):
        if tag in ('td','th') and self.cell is not None:
            self.row.append(' '.join(''.join(self.cell).split())); self.cell = None
        elif tag == 'tr' and self.row is not None:
            if self.cell is not None: raise Failure('schema')
            self.table.append(self.row); self.row = None
        elif tag == 'table' and self.table is not None:
            if self.row is not None: raise Failure('schema')
            self.tables.append(self.table); self.table = None


def money(value):
    if not isinstance(value, str) or not re.fullmatch(r'\$?(?:0|[1-9]\d*|[1-9]\d{0,2}(?:,\d{3})+)\.\d{2}',value):
        raise Failure('invalid_money')
    try: return Decimal(value.replace('$','').replace(',',''))
    except InvalidOperation: raise Failure('invalid_money') from None


def parse_tax(raw, record):
    # Generic adapter: must be calibrated to the approved portal before activation.
    if re.search(r'captcha|verify you are human|access denied|sign in to continue',raw,re.I):
        raise Failure('access_denied')
    p = Tables()
    try: p.feed(raw); p.close()
    except (ValueError, TypeError): raise Failure('schema') from None
    if p.table is not None: raise Failure('schema')
    expected = record['headers']
    matches = [t for t in p.tables if t and t[0] == expected]
    if not matches: raise Failure('schema')
    if len(matches) != 1: raise Failure('ambiguous')
    rows = matches[0][1:]
    if not rows: raise Failure('empty')
    if any(len(r) != len(expected) for r in rows): raise Failure('schema')
    # A configured detail table must contain ONLY this parcel's complete year history.
    idx = {k: expected.index(record['columns'][k]) for k in ('parcel','year','balance')}
    if any(r[idx['parcel']] != record['parcel'] for r in rows): raise Failure('identity_mismatch')
    years = [r[idx['year']] for r in rows]
    if any(not re.fullmatch(r'20\d{2}',y) for y in years): raise Failure('schema')
    if len(years) != len(set(years)): raise Failure('ambiguous')
    if str(record['required_year']) not in years: raise Failure('stale')
    total = sum((money(r[idx['balance']]) for r in rows),Decimal('0.00'))
    return {'observed': format(total,'.2f'), 'latest_year': max(years), 'status': 'ZERO' if total == 0 else 'BALANCE_NONZERO'}


def validate_config(c):
    if not isinstance(c,dict) or c.get('schema_version') != 1: raise ValueError()
    for kind in ('owners','taxes'):
        if not isinstance(c.get(kind),list) or len(c[kind]) != 6: raise ValueError()
    ids = []
    for r in c['owners'] + c['taxes']:
        if not isinstance(r,dict) or not re.fullmatch(r'[a-z][a-z0-9_]{0,31}',r.get('id','')): raise ValueError()
        ids.append(r['id'])
        if r.get('county') != 'Stewart': raise ValueError()
    if len(ids) != len(set(ids)): raise ValueError()
    for r in c['owners']:
        if not re.fullmatch(r'081\d{3}[A-Z]{0,2} [A-Z]{0,2} \d{5}',r.get('parcel','')): raise ValueError()
        if not isinstance(r.get('expected'),str) or not r['expected'].strip(): raise ValueError()
    for r in c['taxes']:
        if not r.get('configured',False): continue
        if r.get('baseline') != '0.00' or r.get('complete_history_verified') is not True: raise ValueError()
        if not isinstance(r.get('parcel'),str) or not r['parcel']: raise ValueError()
        u = urllib.parse.urlsplit(r.get('url',''))
        # Fixed official host allowlist, not a general URL fetcher.
        if u.scheme != 'https' or u.hostname != 'tennesseetrustee.org' or u.username or u.password or u.port not in (None,443): raise ValueError()
        if u.fragment: raise ValueError()
        if not isinstance(r.get('headers'),list) or len(set(r['headers'])) != len(r['headers']): raise ValueError()
        if any(not isinstance(h,str) or not h for h in r['headers']): raise ValueError()
        if set(r.get('columns',{})) != {'parcel','year','balance'}: raise ValueError()
        if any(h not in r['headers'] for h in r['columns'].values()): raise ValueError()
        if len(set(r['columns'].values())) != 3: raise ValueError()
        if type(r.get('required_year')) is not int or not 2000 <= r['required_year'] <= 2099: raise ValueError()
    return c


def check(record, adapter, fixtures, blocked=None):
    result = {'lookup_attempted':False, 'id':record['id'], 'adapter':adapter, 'parcel':record.get('parcel'), 'expected':record.get('expected','0.00'), 'checked_at':datetime.now(timezone.utc).isoformat()}
    try:
        if adapter == 'tax_table' and not record.get('configured'):
            raise Failure('unconfigured')
        if not fixtures and blocked is not None and adapter in blocked:
            raise Failure('access_denied')
        if fixtures:
            result['lookup_attempted']=True
            path = fixtures / (record['id'] + ('.json' if adapter == 'tnmap_owner' else '.html'))
            try: raw = path.read_text(encoding='utf-8')
            except OSError: raise Failure('empty') from None
            result['source'] = 'offline fixture'
        elif adapter == 'tnmap_owner':
            url = OWNER_URL + '?' + urllib.parse.urlencode({'where':"GISLINK = '"+record['parcel']+"'",'outFields':'GISLINK,OWNER','returnGeometry':'false','f':'json'})
            result['source'] = 'https://tnmap.tn.gov/assessment/#/parcel/' + urllib.parse.quote(record['parcel'])
            result['lookup_attempted']=True
            raw = fetch(url,'application/json')
        else:
            result['source'] = record['url']
            result['lookup_attempted']=True
            raw = fetch(record['url'],'text/html')
        result.update(parse_owner(raw,record) if adapter == 'tnmap_owner' else parse_tax(raw,record))
    except Failure as e:
        if e.code == 'access_denied' and not fixtures and blocked is not None:
            blocked.add(adapter)
        result.update(status='UNVERIFIABLE', error=e.code, observed=None)
    return result


def report(results, offline):
    unknown = sum(r['status']=='UNVERIFIABLE' for r in results)
    changes = sum(r['status'] in ('OWNER_CHANGE','BALANCE_NONZERO') for r in results)
    taxes = [r for r in results if r['adapter']=='tax_table']
    total = None if any(r['status']=='UNVERIFIABLE' for r in taxes) else format(sum((Decimal(r['observed']) for r in taxes),Decimal('0.00')),'.2f')
    known = format(sum((Decimal(r['observed']) for r in taxes if r['observed'] is not None),Decimal('0.00')),'.2f')
    return {'schema_version':1,'version':VERSION,'mode':'OFFLINE TEST' if offline else 'READ-ONLY LIVE', 'checked_at':datetime.now(ZoneInfo('America/Chicago')).isoformat(), 'status':'PARTIAL' if unknown else ('ALERT' if changes else 'ALL_CLEAR'),'unverifiable':unknown,'alerts':changes,'tax_total':total,'known_tax_subtotal':known,'results':results}


def render(rep):
    lines = [f"Stewart Property Watch — {rep['status']} ({rep['mode']})", f"Checked: {rep['checked_at']} [America/Chicago]", f"Unverifiable checks: {rep['unverifiable']}; owner/nonzero-balance alerts: {rep['alerts']}", 'Tax total: '+('$'+rep['tax_total'] if rep['tax_total'] is not None else 'UNKNOWN; known subtotal $'+rep['known_tax_subtotal']+' (incomplete)'), '']
    for r in rep['results']:
        lines.append(f"{r['id']} | {r.get('parcel') or 'identity not recovered'} | expected {r['expected']} | observed {r['observed'] if r['observed'] is not None else 'UNKNOWN'} | {r['status']}" + (f" ({r['error']})" if 'error' in r else ''))
        lines.append(f"Checked: {r['checked_at']} | Source: {r.get('source','not configured')}")
    lines += ['', 'Owner fields are compared exactly with the configured primary-owner baseline. Tax balances are compared with $0.00, not last week. Source failure is not an ownership or balance change.', 'Public assessment records are informational, not legal proof of title. No payment action is performed.']
    text = '\n'.join(lines)+'\n'
    return text, '<!doctype html><html lang="en"><meta charset="utf-8"><title>Stewart Property Watch</title><body><pre style="white-space:pre-wrap">'+escape(text)+'</pre></body></html>'


def queue_repairs(rep, state):
    """Local durable outbox: fixed enums ONLY; no source content or personal identifiers."""
    failures = sorted({(r['adapter'],r['error']) for r in rep['results'] if r['status']=='UNVERIFIABLE' and r['error']!='unconfigured'})
    queue = state / 'repair-outbox'
    count = 0
    for adapter,code in failures:
        event = {'schema_version':1,'project':'stewart-property-watch','adapter':adapter,'failure_code':code,'parser_version':VERSION}
        event_id = hashlib.sha256(json.dumps(event,sort_keys=True).encode()).hexdigest()
        path = queue / (event_id+'.json')
        if not path.exists():
            private_write(path,json.dumps(dict(event,event_id=event_id),indent=2)+'\n'); count += 1
    return count


def send_email(rep, text, html, config, state):
    """Opt-in only. Store pending BEFORE SMTP; uncertain delivery requires manual review."""
    if not config.get('delivery_approved') or rep['mode']!='READ-ONLY LIVE': raise ValueError()
    mail = config['mail']
    sender, recipients = mail['sender'], mail['recipients']
    if not isinstance(recipients,list) or not recipients: raise ValueError()
    for address in [sender]+recipients:
        if not isinstance(address,str) or not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+',address): raise ValueError()
    if not isinstance(mail['host'],str) or not mail['host'] or int(mail.get('port',465)) != 465: raise ValueError()
    week = datetime.now(ZoneInfo('America/Chicago')).strftime('%G-W%V')
    ledger = state / ('delivery-'+week+'.json')
    if ledger.exists(): raise ValueError()
    msg = EmailMessage(); msg['From']=sender; msg['To']=', '.join(recipients)
    msg['Subject']=f"Stewart Property Watch — {rep['status']} — {week}"
    msg['Date']=format_datetime(datetime.now(timezone.utc)); msg['Message-ID']=f'<stewart-{week}@{sender.split("@")[1]}>'
    msg.set_content(text); msg.add_alternative(html,subtype='html')
    password = os.environ['STEWART_SMTP_PASSWORD']; user = os.environ['STEWART_SMTP_USER']
    private_write(ledger,json.dumps({'status':'pending_or_uncertain'}))
    with smtplib.SMTP_SSL(mail['host'],465,timeout=30,context=ssl.create_default_context()) as smtp:
        smtp.login(user,password)
        refused = smtp.send_message(msg)
        if refused: raise ValueError()
    private_write(ledger,json.dumps({'status':'smtp_accepted_not_inbox_verified'}))


def summary(rep=None, mail='not_attempted', queued=0, complete=False, run_id=None):
    rows = rep['results'] if rep else []
    statuses = ('MATCH','OWNER_CHANGE','ZERO','BALANCE_NONZERO','UNVERIFIABLE')
    return {'schema_version':1,'program':'stewart-property-watch','version':VERSION,
            'run_id':run_id,'report_complete':complete,'run_status':rep['status'] if rep else 'FAILED',
            'expected_checks':12,'completed_checks':len(rows),
            'source_lookups_attempted':sum(r.get('lookup_attempted',False) for r in rows),
            'owner_checks':sum(r['adapter']=='tnmap_owner' for r in rows),
            'tax_checks':sum(r['adapter']=='tax_table' for r in rows),
            'ownership':{'expected':6,'verified':sum(r['adapter']=='tnmap_owner' and r['status']!='UNVERIFIABLE' for r in rows)},
            'taxes':{'expected':6,'verified':sum(r['adapter']=='tax_table' and r['status']!='UNVERIFIABLE' for r in rows)},
            'counts':{s:sum(r['status']==s for r in rows) for s in statuses},
            'delivery':mail,'repair_events_created':queued}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-id',type=uuid.UUID)
    p.add_argument('--config',required=True,type=Path)
    p.add_argument('--output',required=True,type=Path)
    p.add_argument('--state',required=True,type=Path)
    p.add_argument('--fixtures',type=Path)
    g=p.add_mutually_exclusive_group(); g.add_argument('--dry-run',action='store_true'); g.add_argument('--send',action='store_true')
    args=p.parse_args(argv)
    os.umask(0o077)
    run_id=str(args.run_id or uuid.uuid4())
    result=summary(run_id=run_id); code=1
    try:
        config=validate_config(json.loads(args.config.read_text()))
        if config.get('fixture_only') and not args.fixtures: raise ValueError()
        if args.send and (args.fixtures or not config.get('delivery_approved')): raise ValueError()
        with lock(args.state / 'run.lock'):
            # Overwrite an old summary before any network or email operation.
            private_write(args.output/'summary.json',json.dumps(result)+'\n')
            blocked=set()
            rows=[check(r,'tnmap_owner',args.fixtures,blocked) for r in config['owners']]+[check(r,'tax_table',args.fixtures,blocked) for r in config['taxes']]
            rep=report(rows,bool(args.fixtures)); text,html=render(rep)
            private_write(args.output/'report.json',json.dumps(rep,indent=2)+'\n')
            private_write(args.output/'report.txt',text); private_write(args.output/'report.html',html)
            queued=queue_repairs(rep,args.state)
            result=summary(rep,'not_requested',queued,True,run_id)
            if args.send:
                result['delivery']='unknown'
                private_write(args.output/'summary.json',json.dumps(result)+'\n')
                send_email(rep,text,html,config,args.state)
                result['delivery']='smtp_accepted'
            code=1 if rep['unverifiable'] else 0
            private_write(args.output/'summary.json',json.dumps(result)+'\n')
    except (OSError,ValueError,KeyError,TypeError,smtplib.SMTPException):
        # Preserve an explicit uncertain mail state if SMTP might have accepted.
        result['run_status']='FAILED'
        try: private_write(args.output/'summary.json',json.dumps(result)+'\n')
        except OSError: pass
        code=1
    print(json.dumps(result,sort_keys=True))
    return code

if __name__=='__main__': sys.exit(main())
