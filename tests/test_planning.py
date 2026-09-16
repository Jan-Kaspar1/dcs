import unittest
from agent_pool.planning import validate, body, metadata, prompt

class PlanningTests(unittest.TestCase):
    def item(self):
        return dict(key='model-contract',title='Define model',scope='Types',acceptance='Compiles',tests='Unit tests',dependencies=[],priority=1,milestone='Foundation',group='model')

    def test_roundtrip(self):
        item=self.item()
        self.assertEqual(validate({'issues':[item]})['issues'],[item])
        self.assertEqual(metadata(body(item))['key'],item['key'])

    def test_reject_duplicates(self):
        with self.assertRaises(ValueError): validate({'issues':[self.item(),self.item()]})

    def test_allows_dependencies_on_tasks_in_the_same_proposal(self):
        contract=self.item()
        consumer=self.item(); consumer['key']='model-consumer'; consumer['dependencies']=['model-contract']
        proposal=validate({'issues':[consumer,contract]})
        self.assertEqual(proposal['issues'][0]['dependencies'],['model-contract'])

    def test_reject_unknown_fields_and_dependencies(self):
        item=self.item(); item['dependencies']=['future']
        with self.assertRaises(ValueError): validate({'issues':[item]})
        with self.assertRaises(ValueError): validate({'issues':[],'execute':'anything'})

    def test_rejects_cyclic_same_proposal_dependencies(self):
        first=self.item(); first['dependencies']=['second']
        second=self.item(); second['key']='second'; second['dependencies']=['model-contract']
        with self.assertRaisesRegex(ValueError, 'cycle'):
            validate({'issues':[first,second]})

    def test_limit(self):
        with self.assertRaises(ValueError): validate({'issues':[self.item()]*21})

    def test_improvement_roundtrip(self):
        item=self.item(); item['improvement']='deepen-executor'
        proposal=validate({'issues':[item]})
        self.assertEqual(metadata(body(item))['improvement'],'deepen-executor')
        item['improvement']='Not A Key!'
        with self.assertRaises(ValueError): validate({'issues':[item]})

    def test_dispositions(self):
        ok={'key':'deepen-executor','decision':'defer','reason':'needs decision','revisit':'after #19'}
        self.assertEqual(validate({'issues':[],'dispositions':[ok]})['dispositions'],[ok])
        with self.assertRaises(ValueError):
            validate({'issues':[],'dispositions':[{'key':'x','decision':'accept','reason':'','revisit':''}]})
        with self.assertRaises(ValueError):
            validate({'issues':[],'dispositions':[{'key':'x','decision':'defer','reason':'vague'}]})
        with self.assertRaises(ValueError):
            validate({'issues':[],'dispositions':[{'key':'x','decision':'maybe','reason':'r'}]})

    def test_prompt_loads_product_context_and_research_gate(self):
        text = prompt([], [], '/tmp/proposal.json')
        self.assertIn('docs/product-strategy.md', text)
        self.assertIn('docs/requirements/README.md', text)
        self.assertIn('docs/research/', text)
        self.assertIn('Requirements: ID, ID', text)
        self.assertIn('documentation-only research issue first', text)
        self.assertIn('WW-ALM-001', text)
        self.assertIn('IJmuiden mode-change/unsafe-position/rising-level scenario', text)
        self.assertIn('`WW-ENG-003` as the immediate product-boundary gate', text)
        self.assertIn('independently published pumping-station repository', text)
        self.assertIn('directory that remains inside this workspace is only staging', text)
        self.assertIn('Platform fixtures are conformance evidence', text)
        self.assertIn('`WW-FND-003` and `WW-FND-004` as the next foundation tranche', text)
        self.assertIn('additive shared block-interface schema first', text)
        self.assertIn('named typed commands and events', text)
        self.assertIn('immutable bounded read publication outside the executor lock', text)
        self.assertIn('bounded receipted command admission', text)
        self.assertIn('stalled/disconnected/restarted-UI non-interference tests', text)
        self.assertIn('never plan fire-and-forget commands', text)
        self.assertIn('direct reuse of LGPL QiTech implementation code', text)
        self.assertIn('batch control as deferred', text)

if __name__ == '__main__': unittest.main()
