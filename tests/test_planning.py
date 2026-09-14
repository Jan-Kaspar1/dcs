import unittest
from agent_pool.planning import validate, body, metadata

class PlanningTests(unittest.TestCase):
    def item(self):
        return dict(key='model-contract',title='Define model',scope='Types',acceptance='Compiles',tests='Unit tests',dependencies=[],priority=1,milestone='Foundation',group='model')

    def test_roundtrip(self):
        item=self.item()
        self.assertEqual(validate({'issues':[item]}),[item])
        self.assertEqual(metadata(body(item))['key'],item['key'])

    def test_reject_duplicates(self):
        with self.assertRaises(ValueError): validate({'issues':[self.item(),self.item()]})

    def test_reject_unknown_fields_and_dependencies(self):
        item=self.item(); item['dependencies']=['future']
        with self.assertRaises(ValueError): validate({'issues':[item]})
        with self.assertRaises(ValueError): validate({'issues':[],'execute':'anything'})

    def test_limit(self):
        with self.assertRaises(ValueError): validate({'issues':[self.item()]*21})

if __name__ == '__main__': unittest.main()
