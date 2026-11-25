import copy
import http.server
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import watch
from supervisor import assess

class WatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name); self.fixtures=self.base/'fixtures'
        shutil.copytree(ROOT/'fixtures',self.fixtures)
        self.config=json.loads((ROOT/'config.example.json').read_text())
        self.cp=self.base/'config.json'; self.save()
    def save(self): self.cp.write_text(json.dumps(self.config))
    def run_cli(self, supervised=False, send=False):
        args=[sys.executable,str(ROOT/('run_supervised.py' if supervised else 'watch.py')),'--config',str(self.cp),'--output',str(self.base/'reports'),'--state',str(self.base/'state'),'--fixtures',str(self.fixtures)]
        args+=['--send' if send else '--dry-run'] if not supervised else (['--send'] if send else [])
        result=subprocess.run(args,capture_output=True,text=True)
        self.assertEqual(result.stderr,'')
        return result,json.loads(result.stdout)
    def test_full_report_and_real_findings(self):
        p,s=self.run_cli(); self.assertEqual(p.returncode,0)
        self.assertEqual(s['counts'],{'MATCH':6,'OWNER_CHANGE':0,'ZERO':0,'BALANCE_NONZERO':6,'UNVERIFIABLE':0})
        self.assertEqual(assess(0,s),'SUCCESS_WITH_FINDINGS')
        rep=json.loads((self.base/'reports/report.json').read_text())
        self.assertEqual(rep['tax_total'],'193.00')
        self.assertEqual(s['delivery'],'not_requested')
        self.assertFalse((self.base/'state/repair-outbox').exists())
        self.assertEqual((self.base/'reports/report.json').stat().st_mode & 0o777,0o600)
    def test_clear_success_supervisor_bounded(self):
        for f in self.fixtures.glob('tax_[1-6].html'):
            import re
            f.write_text(re.sub(r'\$\d+\.00','$0.00',f.read_text()))
        p,s=self.run_cli(supervised=True)
        self.assertEqual(p.returncode,0); self.assertEqual(s['verdict'],'SUCCESS')
        self.assertLess(len(p.stdout),512); self.assertNotIn('SYNTHETIC',p.stdout)
    def test_supervisor_findings(self):
        p,s=self.run_cli(supervised=True)
        self.assertEqual(s['verdict'],'SUCCESS_WITH_FINDINGS'); self.assertEqual(p.returncode,0)
    def test_drift_partial_and_dedup_local_handoff(self):
        shutil.copyfile(self.fixtures/'tax-drift.html',self.fixtures/'tax_1.html')
        p,s=self.run_cli(); self.assertEqual(p.returncode,1)
        self.assertEqual(assess(1,s),'PARTIAL'); self.assertEqual(s['repair_events_created'],1)
        rep=json.loads((self.base/'reports/report.json').read_text())
        self.assertIsNone(rep['tax_total']); self.assertEqual(rep['known_tax_subtotal'],'173.00')
        self.assertEqual(self.run_cli()[1]['repair_events_created'],0)
        received=[]
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(202);self.end_headers()
            def log_message(self,*args): pass
        server=http.server.HTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            cmd=[sys.executable,str(ROOT/'repair_stub_relay.py'),'--state',str(self.base/'state'),'--endpoint',f'http://127.0.0.1:{server.server_port}/repair']
            for _ in range(2):
                r=subprocess.run(cmd,capture_output=True,text=True);self.assertEqual(r.returncode,0,r.stderr)
            self.assertEqual(len(received),1)
            self.assertEqual(set(received[0]),{'schema_version','project','adapter','failure_code','parser_version','event_id'})
            self.assertEqual(received[0]['failure_code'],'schema')
            self.assertNotIn('publish',json.dumps(received));self.assertNotIn('TEST-1',json.dumps(received))
        finally:server.shutdown();server.server_close();thread.join()
    def test_partial_supervisor(self):
        (self.fixtures/'owner_1.json').write_text('{"features":[]}')
        p,s=self.run_cli(supervised=True);self.assertEqual(s['verdict'],'PARTIAL');self.assertEqual(p.returncode,1)
    def test_config_failure_summary(self):
        self.config['owners']=[];self.save()
        p,s=self.run_cli();self.assertEqual(p.returncode,1);self.assertFalse(s['report_complete'])
        self.assertEqual(assess(1,s),'FAILED')
        p,s=self.run_cli(supervised=True);self.assertEqual(s['verdict'],'FAILED')
    def test_send_guard_no_transport(self):
        p,s=self.run_cli(send=True);self.assertEqual(p.returncode,1)
        self.assertEqual(s['delivery'],'not_attempted')
    def test_unknown_delivery_never_success(self):
        _,s=self.run_cli();s['delivery']='unknown'
        self.assertEqual(assess(0,s,True),'DELIVERY_UNVERIFIED')
        self.assertEqual(assess(0,{},True),'FAILED')
    def test_owner_validation_cases(self):
        r=self.config['owners'][0]
        cases=[('{"features":[]}','empty'),('{"features":[{},{}]}','ambiguous'),('{}','schema'),((self.fixtures/'owner-access-denied.json').read_text(),'access_denied')]
        for raw,code in cases:
            with self.subTest(code=code),self.assertRaises(watch.Failure) as cm:watch.parse_owner(raw,r)
            self.assertEqual(cm.exception.code,code)
        data={'features':[{'attributes':{'GISLINK':'WRONG','OWNER':'SYNTHETIC OWNER'}}]}
        with self.assertRaises(watch.Failure) as cm:watch.parse_owner(json.dumps(data),r)
        self.assertEqual(cm.exception.code,'identity_mismatch')
        data['features'][0]['attributes']={'GISLINK':r['parcel'],'OWNER':'synthetic owner'}
        self.assertEqual(watch.parse_owner(json.dumps(data),r)['status'],'OWNER_CHANGE')
    def test_tax_validation_cases(self):
        r=self.config['taxes'][0]; raw=(self.fixtures/'tax_1.html').read_text()
        for changed,code in [(raw.replace('TEST-1','TEST-2'),'identity_mismatch'),(raw.replace('$20.00',''),'invalid_money'),(raw.replace('2026','2025'),'stale'),(raw+raw,'ambiguous'),('<table><tr><th>Parcel</th><th>Year</th><th>Balance</th></tr></table>','empty')]:
            with self.subTest(code=code),self.assertRaises(watch.Failure) as cm:watch.parse_tax(changed,r)
            self.assertEqual(cm.exception.code,code)
    def test_money_rejects_nonfinite_and_locale(self):
        for v in ('NaN','Infinity','1,23.00','-1.00','0','',None):
            with self.assertRaises(watch.Failure):watch.money(v)
        self.assertEqual(str(watch.money('$1,234.56')),'1234.56')
    def test_transient_retries_bounded_access_not_retried(self):
        class Opener:
            def __init__(self,code):self.calls=0;self.code=code
            def open(self,*a,**kw):
                self.calls+=1;raise urllib.error.HTTPError('https://example.invalid',self.code,'',{},None)
        for code,calls,reason in [(503,3,'http_error'),(403,1,'access_denied'),(429,1,'access_denied')]:
            o=Opener(code)
            with self.assertRaises(watch.Failure) as cm:watch.fetch('https://example.invalid','text/html',o,lambda _:None)
            self.assertEqual(o.calls,calls);self.assertEqual(cm.exception.code,reason)
    def test_redaction_and_escaping(self):
        r=self.config['owners'][0]
        raw=json.dumps({'features':[{'attributes':{'GISLINK':r['parcel'],'OWNER':'<script>private@example.invalid</script>'}}]})
        (self.fixtures/'owner_1.json').write_text(raw)
        p,s=self.run_cli();self.assertNotIn('private@',p.stdout)
        html=(self.base/'reports/report.html').read_text()
        self.assertNotIn('<script>',html);self.assertIn('&lt;script&gt;',html)
    def test_unconfigured_never_zero(self):
        self.config['taxes'][0]={'id':'tax_1','county':'Stewart','configured':False};self.save()
        p,s=self.run_cli();self.assertEqual(p.returncode,1)
        rep=json.loads((self.base/'reports/report.json').read_text());self.assertIsNone(rep['tax_total'])
        self.assertEqual(s['repair_events_created'],0)
    def test_smtp_mock_acceptance_and_no_duplicate(self):
        _,s=self.run_cli();rep=json.loads((self.base/'reports/report.json').read_text());rep['mode']='READ-ONLY LIVE'
        config={'delivery_approved':True,'mail':{'sender':'sender@example.invalid','recipients':['recipient@example.invalid'],'host':'smtp.example.invalid'}}
        text,html=watch.render(rep)
        with patch.dict('os.environ',{'STEWART_SMTP_PASSWORD':'synthetic','STEWART_SMTP_USER':'synthetic'}),patch('watch.smtplib.SMTP_SSL') as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value={}
            watch.send_email(rep,text,html,config,self.base/'mailstate')
            with self.assertRaises(ValueError):watch.send_email(rep,text,html,config,self.base/'mailstate')
            self.assertEqual(smtp.call_count,1)
    def test_smtp_unknown_no_automatic_retry(self):
        _,s=self.run_cli();rep=json.loads((self.base/'reports/report.json').read_text());rep['mode']='READ-ONLY LIVE'
        config={'delivery_approved':True,'mail':{'sender':'sender@example.invalid','recipients':['recipient@example.invalid'],'host':'smtp.example.invalid'}}
        text,html=watch.render(rep)
        with patch.dict('os.environ',{'STEWART_SMTP_PASSWORD':'synthetic','STEWART_SMTP_USER':'synthetic'}),patch('watch.smtplib.SMTP_SSL',side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):watch.send_email(rep,text,html,config,self.base/'mailstate')
            with self.assertRaises(ValueError):watch.send_email(rep,text,html,config,self.base/'mailstate')

    def test_access_circuit_and_request_count(self):
        blocked=set()
        with patch('watch.fetch',return_value=(self.fixtures/'owner-access-denied.json').read_text()) as fetch:
            results=[watch.check(r,'tnmap_owner',None,blocked) for r in self.config['owners']]
        self.assertEqual(fetch.call_count,1)
        self.assertTrue(all(r['status']=='UNVERIFIABLE' for r in results))
        self.assertEqual(sum(r['lookup_attempted'] for r in results),1)
    def test_external_config_and_run_id(self):
        self.assertNotEqual(self.cp.parent,ROOT)
        p,s=self.run_cli(supervised=True)
        import uuid
        uuid.UUID(s['run_id'])
        self.assertEqual(s['owners'],{'expected':6,'verified':6,'changed':0})
        self.assertEqual(s['taxes'],{'expected':6,'verified':6,'nonzero':6})
        self.assertLess(len(p.stdout),512)
    def test_fixture_only_config_refuses_live(self):
        p=subprocess.run([sys.executable,str(ROOT/'watch.py'),'--config',str(self.cp),'--output',str(self.base/'reports'),'--state',str(self.base/'state')],capture_output=True,text=True)
        self.assertEqual(p.returncode,1)
        self.assertFalse(json.loads(p.stdout)['report_complete'])

if __name__=='__main__':unittest.main()
