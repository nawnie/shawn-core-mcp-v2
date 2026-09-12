"""Regression coverage for routine and cross-domain personal gateway work."""
import ast
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import nawnie_server as core


class CoreFlexibilityTest(unittest.TestCase):
    def owners(self, prompt):
        return {item['specialist'] for item in core.specialist_handoffs(prompt)}

    def test_aliases_do_not_match_email_or_longer_names(self):
        for prompt in ('email ted@ted.example about syntax', '@tedious summarize this', '@alice summarize this'):
            with self.subTest(prompt=prompt):
                self.assertFalse(any(item['explicit'] for item in core.specialist_handoffs(prompt)))
        self.assertIn('ted', self.owners('@ted, check this ledger'))

    def test_generic_terms_do_not_route_to_unrelated_owners(self):
        for prompt, unrelated in (
            ('Explain Python syntax', 'ted'),
            ('Discuss an academic calendar', 'cad'),
            ('Inspect JSON content', 'victoria'),
            ('Review the audit results', 'agent-t'),
            ('Explain a token budget', 'ted'),
            ('Read the release notes', 'agent-t'),
        ):
            with self.subTest(prompt=prompt):
                self.assertNotIn(unrelated, self.owners(prompt))
        self.assertIn('token-master', self.owners('Explain a token budget'))
        self.assertIn('ted', self.owners('Compare the token budget and finance budget'))
        self.assertIn('agent-t', self.owners('Perform a security audit'))
        self.assertIn('victoria', self.owners('Write a website content strategy'))

    def test_no_cross_domain_owner_disappears_after_four(self):
        prompt = 'Implement finance marketing security hotel CUDA CAD frontend design'
        all_owners = self.owners(prompt)
        active, deferred = core._specialist_phases(prompt)
        self.assertEqual(len(active), 4)
        self.assertIn('verifier', {item['specialist'] for item in active})
        self.assertEqual(all_owners, {item['specialist'] for item in active + deferred})
        self.assertTrue(deferred)
        self.assertFalse({item['specialist'] for item in active} & {item['specialist'] for item in deferred})

    def test_filename_extensions_and_hyphenated_domains_still_route(self):
        self.assertIn('creation-kit', self.owners('Inspect MyMod.esm'))
        self.assertIn('operator', self.owners('Review startup.ps1'))
        self.assertIn('al', self.owners('Explain CUDA-based inference'))
        self.assertNotIn('creation-kit', self.owners('Explain something.esmeralda'))

    def test_explicit_multi_owner_scope_retained(self):
        active, deferred = core._specialist_phases('@ted @victoria @wren @cad @al implement this')
        self.assertEqual({item['specialist'] for item in active + deferred}, {'ted', 'victoria', 'wren', 'cad', 'al', 'verifier'})

    def test_compact_preserves_acceptance_and_deferred_work(self):
        args = {'task': 'Implement finance marketing security hotel CUDA CAD frontend design', 'constraints': ['Local only'], 'acceptance_criteria': ['Tests pass']}
        full = core.tool_core_query(args)
        compact = core.tool_core_query({**args, 'detail': 'compact'})
        for field in ('request_id', 'task', 'constraints', 'acceptance_criteria', 'validation_contract', 'server_creation_gate', 'capability_fallback'):
            self.assertEqual(full[field], compact[field])
        for field in ('specialist_plan', 'deferred_specialists'):
            self.assertEqual([item['specialist'] for item in full[field]], [item['specialist'] for item in compact[field]])
        self.assertNotIn('routing_visualization', compact)
        self.assertLess(len(json.dumps(compact)), len(json.dumps(full)) * 0.75)

    def test_summary_is_owned_searchable_and_schema_can_be_retrieved(self):
        metadata = {'tools': [
            {'name': 'verifier_status', 'description': 'Inspect status', 'inputSchema': {'type': 'object'}},
            {'name': 'verifier_verdict', 'description': 'Issue verdict', 'inputSchema': {'type': 'object'}},
            {'name': 'researcher_status', 'description': 'Inspect status', 'inputSchema': {'type': 'object'}},
        ]}
        with patch.object(core, '_child_mcp_exchange', return_value=metadata):
            summary = core.tool_specialist_tools({'specialist': 'verifier', 'detail': 'summary', 'limit': 1})
            self.assertEqual(summary['total_tool_count'], 2)
            self.assertTrue(summary['truncated'])
            self.assertNotIn('inputSchema', summary['tools'][0])
            full = core.tool_specialist_tools({'specialist': 'verifier', 'query': 'verifier_verdict'})
            self.assertEqual(full['tool_count'], 1)
            self.assertIn('inputSchema', full['tools'][0])
            absent = core.tool_specialist_tools({'specialist': 'verifier', 'query': 'missing'})
            self.assertEqual(absent['tools'], [])

    def test_wrong_shared_owner_rejected_before_execution(self):
        with patch.object(core, '_child_mcp_exchange') as child:
            with self.assertRaisesRegex(core.NawnieError, 'not owned'):
                core.tool_specialist_execute({'specialist': 'verifier', 'tool': 'researcher_status'})
            child.assert_not_called()

    def test_failed_child_retains_reason_and_can_recover(self):
        with patch.object(core, '_child_mcp_exchange', side_effect=[
            {'isError': True, 'content': [{'type': 'text', 'text': 'Required evidence file is missing'}]},
            {'isError': False, 'content': []},
        ]):
            with self.assertRaisesRegex(core.NawnieError, 'Required evidence file is missing'):
                core.tool_specialist_execute({'specialist': 'verifier', 'tool': 'verifier_status'})
            result = core.tool_specialist_execute({'specialist': 'verifier', 'tool': 'verifier_status'})
            self.assertFalse(result['result']['isError'])

    def test_invalid_discovery_options_rejected_without_child_launch(self):
        with patch.object(core, '_child_mcp_exchange') as child:
            for options in ({'limit': True}, {'limit': 0}, {'query': []}, {'detail': []}, {'detail': 'invalid'}):
                with self.subTest(options=options), self.assertRaises(core.NawnieError):
                    core.tool_specialist_tools({'specialist': 'verifier', **options})
            child.assert_not_called()

    def test_windows_flags_cover_every_core_subprocess_site(self):
        flags = core.hidden_subprocess_kwargs()
        self.assertEqual(flags.get('creationflags'), core.subprocess.CREATE_NO_WINDOW)
        tree = ast.parse(Path(core.__file__).read_text(encoding='utf-8'))
        sites = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == 'subprocess' and node.func.attr in {'run', 'Popen'}]
        self.assertGreater(len(sites), 0)
        for site in sites:
            self.assertTrue(any(key.arg is None and isinstance(key.value, ast.Call) and isinstance(key.value.func, ast.Name) and key.value.func.id == 'hidden_subprocess_kwargs' for key in site.keywords), site.lineno)


if __name__ == '__main__':
    unittest.main(verbosity=2)
