import unittest
from agent_pool.planning import validate, body, metadata

class PlanningTests(unittest.TestCase):
    def item(self):
        return dict(key='model-contract',title='Define model',scope='Types',acceptance='Compiles',tests='Unit tests',dependencies=[],priority=1,milestone='Foundation',group='model')

    def test_roundtrip(self):
        item=self.item()
        self.assertEqual(validate({'issues':[item]})['issues'],[item])
        self.assertEqual(metadata(body(item))['key'],item['key'])

    def test_reject_duplicates(self):
        with self.assertRaises(ValueError): validate({'issues':[self.item(),self.item()]})

    def test_reject_unknown_fields_and_dependencies(self):
        item=self.item(); item['dependencies']=['future']
        with self.assertRaises(ValueError): validate({'issues':[item]})
        with self.assertRaises(ValueError): validate({'issues':[],'execute':'anything'})

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

if __name__ == '__main__': unittest.main()
