"""Session discovery and onboarding checks; never contact live agents."""
import argparse
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

loader = importlib.machinery.SourceFileLoader(
    'ai_relay_discovery', str(Path(__file__).resolve().parents[1] / 'ai-relay'))
spec = importlib.util.spec_from_loader(loader.name, loader)
relay = importlib.util.module_from_spec(spec)
loader.exec_module(relay)


def session(kind, sid, cwd, ready=True):
    return {'agent': kind, 'id': sid, 'cwd': str(cwd), 'status': 'idle',
            'name': 'Review changes', 'ready': ready}


class DiscoveryTests(unittest.TestCase):
    def test_claude_ignores_dead_processes_and_background_agents(self):
        agents = [
            {'pid': 1, 'kind': 'interactive', 'sessionId': 'live', 'cwd': '/repo'},
            {'pid': 2, 'kind': 'interactive', 'sessionId': 'closed', 'cwd': '/repo'},
            {'pid': 3, 'kind': 'background', 'sessionId': 'worker', 'cwd': '/repo'},
        ]
        result = subprocess.CompletedProcess([], 0, json.dumps(agents), '')
        with patch.object(relay.subprocess, 'run', return_value=result), \
                patch.object(relay.os, 'kill', side_effect=[None, ProcessLookupError()]):
            self.assertEqual([s['id'] for s in relay.discover_claude()], ['live'])

    def test_codex_lists_blank_threads_but_does_not_select_them(self):
        client = MagicMock()
        client.call.return_value = {'data': ['live', 'blank', 'old', 'worker', 'closing']}
        client.thread.side_effect = [
            {'id': 'live', 'cwd': '/repo', 'status': {'type': 'active'}, 'preview': 'Review', 'name': 'My review'},
            {'id': 'blank', 'cwd': '/repo', 'status': {'type': 'idle'}, 'preview': ''},
            {'id': 'old', 'cwd': '/repo', 'status': {'type': 'notLoaded'}, 'preview': 'Old'},
            {'id': 'worker', 'cwd': '/repo', 'status': {'type': 'active'},
             'preview': 'Task', 'canAcceptDirectInput': False},
            RuntimeError('thread disappeared'),
        ]
        with patch.object(relay, 'CodexClient', return_value=client):
            sessions = relay.discover_codex()
        self.assertEqual([s['id'] for s in sessions], ['live', 'blank'])
        self.assertEqual([s['ready'] for s in sessions], [True, False])
        self.assertEqual(sessions[0]['name'], 'My review')
        client.close.assert_called_once()
        self.assertTrue(all(call.args[0] != 'turn/start' for call in client.call.call_args_list))

    def test_current_checkout_includes_nested_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = root / 'subdir'
            child.mkdir()
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            sessions = [session('Claude', 'same-repo', child),
                        session('Claude', 'other', '/different-checkout')]
            self.assertEqual(relay.choose_session('Claude', sessions, root), 'same-repo')

    def test_multiple_matches_require_selection_instead_of_guessing(self):
        sessions = [session('Codex', 'one', '/repo'), session('Codex', 'two', '/repo')]
        with patch.object(relay, 'checkout', side_effect=lambda p: Path(p)), \
                patch.object(relay.sys.stdin, 'isatty', return_value=False):
            with self.assertRaisesRegex(ValueError, '--codex SESSION_UUID'):
                relay.choose_session('Codex', sessions, '/repo')
        with patch.object(relay, 'checkout', side_effect=lambda p: Path(p)), \
                patch.object(relay.sys.stdin, 'isatty', return_value=True), \
                patch('builtins.input', side_effect=['0', '2']), \
                patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(relay.choose_session('Codex', sessions, '/repo'), 'two')

    def test_other_checkout_and_blank_session_are_not_auto_linked(self):
        with patch.object(relay, 'checkout', side_effect=lambda p: Path(p)), \
                patch.object(relay.sys.stdin, 'isatty', return_value=False):
            with self.assertRaisesRegex(ValueError, 'Cannot automatically choose'):
                relay.choose_session('Codex', [session('Codex', 'other', '/other')], '/repo')
            with self.assertRaisesRegex(ValueError, 'Send one prompt'):
                relay.choose_session('Codex', [session('Codex', 'blank', '/repo', False)], '/repo')

    def test_missing_rollout_fails_before_settings_or_link_are_written(self):
        client = MagicMock()
        client.call.side_effect = RuntimeError('thread/resume: no rollout found for thread id')
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(relay, 'CodexClient', return_value=client), \
                patch.object(relay, 'terminal_for_claude', return_value=None), \
                patch.object(relay, 'write_json') as write:
            args = argparse.Namespace(claude='11111111-1111-4111-8111-111111111111',
                                      codex='22222222-2222-4222-8222-222222222222',
                                      repo=tmp, rounds=3, timeout=600)
            with self.assertRaisesRegex(ValueError, 'send one prompt'):
                relay.install(args)
            write.assert_not_called()
        client.close.assert_called_once()

    def test_partial_discovery_failure_preserves_other_agent_results(self):
        found = [session('Claude', 'live', '/repo')]
        with patch.object(relay, 'discover_claude', return_value=found), \
                patch.object(relay, 'discover_codex', side_effect=OSError('socket unavailable')):
            sessions, problems = relay.discover_sessions()
        self.assertEqual(sessions, found)
        self.assertEqual(len(problems), 1)
        self.assertIn('Codex', problems[0])

    def test_link_without_ids_detects_pair_and_defaults_to_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            child = root / 'subdir'
            child.mkdir()
            claude_id = '11111111-1111-4111-8111-111111111111'
            codex_id = '22222222-2222-4222-8222-222222222222'
            sessions = [session('Claude', claude_id, root), session('Codex', codex_id, child)]
            client = MagicMock()
            client.thread.return_value = {'id': codex_id, 'cwd': str(child), 'status': {'type': 'idle'}}
            args = argparse.Namespace(claude=None, codex=None, repo=None, rounds=3, timeout=600)
            state = root / 'relay-state'
            with patch.object(relay.Path, 'cwd', return_value=child), \
                    patch.object(relay.Path, 'home', return_value=root / 'test-home'), \
                    patch.object(relay, 'ROOT', state), \
                    patch.object(relay, 'CONFIG', state / 'config.json'), \
                    patch.object(relay, 'discover_sessions', return_value=(sessions, [])), \
                    patch.object(relay, 'CodexClient', return_value=client), \
                    patch.object(relay, 'terminal_for_claude', return_value=None), \
                    patch.object(relay, 'start_worker') as startup, \
                    patch('sys.stdout', new_callable=io.StringIO):
                relay.install(args)
                config = relay.read_json(relay.CONFIG)
            self.assertEqual(config['claude'], claude_id)
            self.assertEqual(config['codex'], codex_id)
            self.assertEqual(config['repo'], str(root))
            startup.assert_called_once_with(config)
            self.assertFalse(any(call.args[0] == 'turn/start' for call in client.call.call_args_list))

    def test_default_repo_requires_checkout(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(relay.Path, 'cwd', return_value=Path(tmp)):
            args = argparse.Namespace(claude=None, codex=None, repo=None, rounds=3, timeout=600)
            with self.assertRaisesRegex(ValueError, 'from a Git checkout'):
                relay.install(args)


if __name__ == '__main__':
    unittest.main()
