import unittest
from agent_pool.planning import validate, body, metadata, prompt, planner_inventory

class PlanningTests(unittest.TestCase):
    def item(self):
        return dict(key='model-contract',title='Define model',scope='Types',acceptance='Compiles',tests='Unit tests',dependencies=[],priority=1,milestone='Foundation',group='model',area='engineering')

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

    def test_tests_list_normalized_to_string(self):
        item=self.item(); item['tests']=['Unit tests','  Wire-shape pins ']
        self.assertEqual(validate({'issues':[item]})['issues'][0]['tests'],'Unit tests\nWire-shape pins')
        item['tests']=[]
        with self.assertRaises(ValueError): validate({'issues':[item]})
        item['tests']=['ok',3]
        with self.assertRaises(ValueError): validate({'issues':[item]})

    def test_improvement_roundtrip(self):
        item=self.item(); item['improvement']='deepen-executor'
        proposal=validate({'issues':[item]})
        self.assertEqual(metadata(body(item))['improvement'],'deepen-executor')
        item['improvement']='Not A Key!'
        with self.assertRaises(ValueError): validate({'issues':[item]})
    def test_rejects_unknown_area(self):
        item = self.item()
        item['area'] = 'water'
        with self.assertRaisesRegex(ValueError, 'product area'):
            validate({'issues': [item]})

    def test_dispositions(self):
        ok={'key':'deepen-executor','decision':'defer','reason':'needs decision','revisit':'after #19'}
        self.assertEqual(validate({'issues':[],'dispositions':[ok]})['dispositions'],[ok])
        with self.assertRaises(ValueError):
            validate({'issues':[],'dispositions':[{'key':'x','decision':'accept','reason':'','revisit':''}]})
        with self.assertRaises(ValueError):
            validate({'issues':[],'dispositions':[{'key':'x','decision':'defer','reason':'vague'}]})
        with self.assertRaises(ValueError):
            validate({'issues':[],'dispositions':[{'key':'x','decision':'maybe','reason':'r'}]})

    def test_prompt_uses_current_product_authority(self):
        text = prompt([], [], '/tmp/proposal.json')
        self.assertIn('docs/product-strategy.md', text)
        self.assertIn('docs/requirements/README.md', text)
        self.assertIn('docs/research/', text)
        self.assertIn('Requirements: ID, ID', text)
        self.assertIn('documented market sequence', text)
        self.assertIn('complete slice', text)
        self.assertIn('secondary investigation signal', text)
        self.assertNotIn('WW-ENG-003` as the immediate product-boundary gate', text)

    def test_planner_inventory_keeps_open_context_and_closed_dedup_key(self):
        item = self.item()
        open_issue = {'number': 1, 'title': item['title'], 'state': 'OPEN',
                      'body': body(item), 'labels': [{'name': 'agent:ready'}]}
        closed_issue = dict(open_issue, number=2, state='CLOSED')
        rows = planner_inventory([open_issue, closed_issue])
        self.assertIn('body', rows[0])
        self.assertEqual(rows[0]['key'], 'model-contract')
        self.assertEqual(rows[1]['key'], 'model-contract')
        self.assertNotIn('body', rows[1])
        self.assertNotIn('labels', rows[1])

    def test_prompt_includes_rejection_feedback(self):
        text = prompt([], [], '/tmp/proposal.json', feedback='Missing or oversized text: tests')
        self.assertIn('rejected during validation', text)
        self.assertIn('Missing or oversized text: tests', text)
        self.assertNotIn('rejected during validation', prompt([], [], '/tmp/proposal.json'))

if __name__ == '__main__': unittest.main()
