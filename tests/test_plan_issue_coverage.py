import unittest

from modport.execution_plan import normalize_execution_plan, retain_inventory_issues


class IssueCoverageTests(unittest.TestCase):
    def test_omissions_remain_unresolved_without_creating_tasks(self):
        plan = normalize_execution_plan({'tasks': [{'id': 'one', 'issue_ids': ['a', 'unknown']}]})
        facts = {'migration_inventory': {'issues': [{'issue_id': 'a'}, {'issue_id': 'b'}]}}
        result = retain_inventory_issues(plan, facts)
        self.assertEqual(['one'], [task['id'] for task in result['tasks']])
        self.assertEqual(['b'], result['issue_coverage']['omitted_issue_ids'])
        self.assertEqual(['unknown'], result['issue_coverage']['unknown_claim_ids'])
        self.assertFalse(result['issue_coverage']['resolution_verified'])
        self.assertEqual('b', result['unresolved_issues'][0]['host_issue']['issue_id'])
        dispatched = normalize_execution_plan(result)
        self.assertEqual(result['unresolved_issues'], dispatched['unresolved_issues'])
        self.assertEqual([{'issue_id': 'b'}], dispatched['tasks'][0]['unresolved_inventory_issues'])

    def test_explicit_unresolved_is_not_claimed_as_work_or_repeated(self):
        plan = normalize_execution_plan({'tasks': [{'id': 'one'}], 'unresolved_issues': ['b']})
        facts = {'migration_inventory': {'issues': [{'issue_id': 'b'}]}}
        result = retain_inventory_issues(plan, facts)
        self.assertEqual([], result['issue_coverage']['omitted_issue_ids'])
        self.assertEqual([], result['issue_coverage']['claimed_issue_ids'])
        self.assertEqual(['b'], result['unresolved_issues'])
