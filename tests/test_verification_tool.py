"""No API calls: verify accounting boundaries and regression evidence from a real run."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('verify_sessions',ROOT/'scripts/verify_sessions.py')
v=importlib.util.module_from_spec(spec);spec.loader.exec_module(v)


class AccountingTests(unittest.TestCase):
    def test_cached_input_counted_once_and_error_usage_preserved(self):
        report={'fresh_sessions':True,'sessions':[{}],'requests':[
            {'path':'/agents/a/run','response':{'usage':{'requests':2,'input_tokens':100,'output_tokens':10,'total_tokens':110}}},
            {'path':'/agents/a/run','status':502,'response':{'detail':{'usage':{'requests':1,'input_tokens':20,'output_tokens':5,'total_tokens':25}}}}],
            'evidence':[{'usage':{'input':30,'output':4,'cacheRead':50,'cacheWrite':0,'totalTokens':84}}]}
        summary=v.usage_summary(report)
        self.assertEqual(summary['recorded_total_tokens'],219)
        self.assertTrue(summary['complete_for_this_run'])
        report['requests'].append({'path':'/agents/a/run'})
        self.assertFalse(v.usage_summary(report)['complete_for_this_run'])

    def test_existing_session_and_missing_transcript_are_not_exact_run_totals(self):
        report={'sessions':[{}],'requests':[]}
        result=v.usage_summary(report)
        self.assertFalse(result['complete_for_this_run'])
        self.assertEqual(len(result['limitations']),2)

    def test_real_evidence_passes_but_removed_execution_fails(self):
        source=ROOT/'reports/sandbox-capabilities-verification.json'
        if not source.exists():self.skipTest('Historical real-run fixture unavailable')
        r=json.loads(source.read_text())
        r['evidence']=[{**s,'id':s['session_id'],'trace':[{'message':m} for m in s['messages']]}
                       for s in r['sandbox_evidence']]
        v.verify_evidence(r)
        broken=copy.deepcopy(r)
        broken['evidence'][0]['trace']=[row for row in broken['evidence'][0]['trace']
            if row['message'].get('toolName')!='bash']
        with self.assertRaises(AssertionError):v.verify_evidence(broken)


if __name__=='__main__':unittest.main()
