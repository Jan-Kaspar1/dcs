import unittest

from agent_pool import areas, planning


def managed(number, area, state='OPEN', ready=True):
    item = dict(key=f'issue-{number}', title=f'Task {number}', scope='s',
                acceptance='a', tests='t', dependencies=[], priority=2,
                milestone='m', group='g', area=area)
    labels = [{'name': areas.label(area)}]
    if ready:
        labels.append({'name': 'agent:ready'})
    return {'number': number, 'title': item['title'], 'body': planning.body(item),
            'state': state, 'labels': labels}


class ProductAreaTests(unittest.TestCase):
    def test_taxonomy_is_complete_and_sums_to_one_portfolio(self):
        self.assertEqual(sum(weight for weight, _ in areas.DEFINITIONS.values()), 100)
        self.assertIn('library', areas.DEFINITIONS)
        self.assertNotIn('water', areas.DEFINITIONS)

    def test_qa_inference_uses_capability_not_validation_venue(self):
        self.assertEqual(areas.infer('reference-plant',
                                     'standby fails to reconverge', ''),
                         'high-availability')
        self.assertEqual(areas.infer('qa_lane', 'alarm latch regression', ''),
                         'alarms-diagnostics')
        self.assertEqual(areas.infer('qa_lane', 'runner cannot parse report', ''),
                         'verification')

    def test_allocation_exposes_backlog_and_favors_underinvested_area(self):
        issues = [managed(1, 'engineering'), managed(2, 'library'),
                  managed(3, 'library', ready=False)]
        jobs = [{'issue': 1, 'status': 'done', 'updated': 20},
                {'issue': 1, 'status': 'done', 'updated': 10},
                {'issue': 2, 'status': 'working', 'updated': 30}]
        allocation = areas.Allocation.from_inventory(issues, jobs)
        self.assertEqual(allocation.summary()['library']['open'], 2)
        self.assertEqual(allocation.summary()['library']['ready'], 1)
        self.assertLess(allocation.score('library'), allocation.score('engineering'))

    def test_issue_area_requires_one_known_label_or_metadata_value(self):
        issue = managed(1, 'operations')
        self.assertEqual(areas.issue_area(issue), 'operations')
        issue['labels'] = [{'name': 'area:operations'}, {'name': 'area:library'}]
        # Conflicting labels fall back to the canonical managed metadata.
        self.assertEqual(areas.issue_area(issue), 'operations')


if __name__ == '__main__':
    unittest.main()
