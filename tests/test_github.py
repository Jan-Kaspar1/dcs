import unittest
import json
from unittest.mock import Mock
from agent_pool.github import GitHub


class GitHubTests(unittest.TestCase):
    def setUp(self):
        self.github = GitHub('owner/repo')

    def test_missing_and_bad_checks_fail_closed(self):
        for value in ('failure','skipped','cancelled','pending',None):
            self.github.check_states = Mock(return_value={'test':value})
            self.assertFalse(self.github.checks_pass('sha',['test']))
        self.github.check_states = Mock(return_value={'test':'success'})
        self.assertFalse(self.github.checks_pass('sha',['test','lint']))
        self.assertFalse(self.github.checks_pass('sha',[]))
        self.assertTrue(self.github.checks_pass('sha',['test']))

    def test_newest_check_and_wrong_sha(self):
        self.github.run = Mock(return_value=json.dumps({'check_runs':[
            {'id':1,'head_sha':'x','name':'test','status':'completed','conclusion':'success'},
            {'id':2,'head_sha':'x','name':'test','status':'in_progress','conclusion':None},
            {'id':3,'head_sha':'other','name':'lint','status':'completed','conclusion':'success'}]}))
        self.assertEqual(self.github.check_states('x'),{'test':'pending'})

    def configure_merge(self):
        row = {'state':'open','draft':False,'merged':False,'head':{'sha':'head'},'base':{'ref':'main'}}
        self.github.pr = Mock(return_value=row)
        self.github.main_sha = Mock(return_value='base')
        self.github.includes_main = Mock(return_value=True)
        self.github.checks_pass = Mock(return_value=True)
        self.github.run = Mock()
        return row

    def test_base_change_blocks(self):
        self.configure_merge()
        self.github.main_sha.side_effect=['base','changed']
        self.assertFalse(self.github.merge(1,['test']))
        self.github.run.assert_not_called()

    def test_head_change_blocks(self):
        row = self.configure_merge()
        self.github.pr.side_effect=[row,{**row,'head':{'sha':'changed'}}]
        self.assertFalse(self.github.merge(1,['test']))
        self.github.run.assert_not_called()

    def test_merge_verifies_result(self):
        row = self.configure_merge()
        self.github.pr.side_effect=[row,row,{**row,'merged':True}]
        self.assertTrue(self.github.merge(1,['test']))
        self.assertIn('--match-head-commit',self.github.run.call_args.args)

    def test_pr_idempotency(self):
        self.github.find_pr=Mock(return_value={'number':7})
        self.github.run=Mock()
        self.assertEqual(self.github.create_pr('branch','title','body'),7)
        self.github.run.assert_not_called()
