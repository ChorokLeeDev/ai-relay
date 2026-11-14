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

    def test_codex_lists_blank_loaded_threads_as_ready_without_model_requests(self):
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
        self.assertEqual([s['ready'] for s in sessions], [True, True])
        self.assertEqual([s['fresh'] for s in sessions], [False, True])
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

    def test_link_does_not_choose_other_checkout_but_accepts_blank_session(self):
        with patch.object(relay, 'checkout', side_effect=lambda p: Path(p)), \
                patch.object(relay.sys.stdin, 'isatty', return_value=False):
            with self.assertRaisesRegex(ValueError, 'Cannot automatically choose'):
                relay.choose_session('Codex', [session('Codex', 'other', '/other')], '/repo')
            self.assertEqual(relay.choose_session('Codex', [session('Codex', 'blank', '/repo')], '/repo'), 'blank')

    def test_fresh_loaded_thread_links_and_queues_setup_without_a_first_prompt(self):
        client = MagicMock()
        client.thread.return_value = {'id': '22222222-2222-4222-8222-222222222222',
                                      'cwd': '/repo', 'status': {'type': 'idle'}}
        client.call.side_effect = RuntimeError('thread/resume: no rollout found for thread id')
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(relay.Path, 'home', return_value=Path(tmp)), \
                patch.object(relay, 'ROOT', Path(tmp) / 'state'), \
                patch.object(relay, 'CONFIG', Path(tmp) / 'state/config.json'), \
                patch.object(relay, 'CodexClient', return_value=client), \
                patch.object(relay, 'terminal_for_claude', return_value=None), \
                patch.object(relay, 'start_worker') as startup, \
                patch('sys.stdout', new_callable=io.StringIO):
            args = argparse.Namespace(claude='11111111-1111-4111-8111-111111111111',
                                      codex='22222222-2222-4222-8222-222222222222',
                                      repo=tmp, rounds=3, timeout=600)
            relay.install(args)
            self.assertEqual(relay.read_json(relay.CONFIG)['codex'], args.codex)
            startup.assert_called_once()
            self.assertFalse(any(call.args[0] == 'turn/start' for call in client.call.call_args_list))
        client.close.assert_called_once()

    def test_unloaded_thread_still_fails_before_settings_are_written(self):
        client = MagicMock()
        client.thread.return_value = {'status': {'type': 'notLoaded'}}
        with patch.object(relay, 'CodexClient', return_value=client), \
                patch.object(relay, 'terminal_for_claude', return_value=None), \
                patch.object(relay, 'write_json') as write:
            args = argparse.Namespace(claude='11111111-1111-4111-8111-111111111111',
                                      codex='22222222-2222-4222-8222-222222222222',
                                      repo='/tmp', rounds=3, timeout=600)
            with self.assertRaisesRegex(ValueError, 'open it before linking'):
                relay.install(args)
            write.assert_not_called()

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

    def test_here_finds_pair_outside_callers_directory_and_excludes_the_caller(self):
        sessions = [session('Codex', 'caller', '/home'),
                    session('Claude', 'new-claude', '/work'), session('Codex', 'blank', '/work')]
        with patch.object(relay, 'checkout', side_effect=lambda p: Path(p)), \
                patch.dict(relay.os.environ, {'CODEX_THREAD_ID': 'caller'}):
            self.assertEqual(relay.choose_pair(sessions), ('/work', 'new-claude', 'blank'))
            sessions.append(session('Claude', 'home-claude', '/home'))
            self.assertEqual(relay.choose_pair(sessions), ('/work', 'new-claude', 'blank'))

    def test_here_prefers_new_pair_to_previous_link(self):
        sessions = [session('Claude', 'old-c', '/old'), session('Codex', 'old-x', '/old'),
                    session('Claude', 'new-c', '/new'), session('Codex', 'new-x', '/new')]
        with patch.object(relay, 'checkout', side_effect=lambda p: Path(p)):
            self.assertEqual(relay.choose_pair(sessions, previous={'claude': 'old-c', 'codex': 'old-x'}),
                             ('/new', 'new-c', 'new-x'))
            for s in sessions:
                s['cwd'] = '/same-workspace'
            self.assertEqual(relay.choose_pair(sessions, previous={'claude': 'old-c', 'codex': 'old-x'}),
                             ('/same-workspace', 'new-c', 'new-x'))

    def test_here_requires_selection_for_multiple_new_pairs(self):
        sessions = [session('Claude', 'c1', '/one'), session('Codex', 'x1', '/one'),
                    session('Claude', 'c2', '/two'), session('Codex', 'x2', '/two')]
        with patch.object(relay, 'checkout', side_effect=lambda p: Path(p)), \
                patch.object(relay.sys.stdin, 'isatty', return_value=False):
            with self.assertRaisesRegex(ValueError, 'Multiple terminal pairs'):
                relay.choose_pair(sessions)
            self.assertEqual(relay.choose_pair(sessions, repo='/two'), ('/two', 'c2', 'x2'))

    def test_here_never_combines_sessions_from_different_workspaces(self):
        sessions = [session('Claude', 'c1', '/one'), session('Codex', 'x1', '/two')]
        with patch.object(relay, 'checkout', side_effect=lambda p: Path(p)):
            with self.assertRaisesRegex(ValueError, 'same workspace'):
                relay.choose_pair(sessions)

    def test_here_connects_non_git_workspace_and_queues_actual_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / 'applications'
            workspace.mkdir()
            task = root / 'task.txt'
            task.write_text('Read the PDF.\nPrepare interview next steps.')
            c = '11111111-1111-4111-8111-111111111111'
            x = '22222222-2222-4222-8222-222222222222'
            sessions = [session('Claude', c, workspace), session('Codex', x, workspace)]
            client = MagicMock()
            client.thread.return_value = {'id': x, 'cwd': str(workspace), 'status': {'type': 'idle'}}
            args = argparse.Namespace(command='here', claude=None, codex=None, repo=None,
                                      initiator='claude', exclude=[], task_file=str(task), rounds=3, timeout=600)
            with patch.object(relay.Path, 'home', return_value=root / 'test-home'), \
                    patch.object(relay, 'ROOT', root / 'state'), \
                    patch.object(relay, 'CONFIG', root / 'state/config.json'), \
                    patch.object(relay, 'discover_sessions', return_value=(sessions, [])), \
                    patch.object(relay, 'CodexClient', return_value=client), \
                    patch.object(relay, 'terminal_for_claude', return_value={'transport': 'iterm', 'pid': 7}), \
                    patch.object(relay, 'start_worker') as start, \
                    patch('sys.stdout', new_callable=io.StringIO):
                relay.install(args)
                config = relay.read_json(relay.CONFIG)
                queued = relay.read_json(relay.ROOT / 'tasks' / (config['task_id'] + '.json'))
                self.assertEqual(config['repo'], str(workspace))
                self.assertEqual(queued['prompt'], task.read_text())
                self.assertEqual(queued['epoch'], relay.read_json(relay.state_path(c))['epoch'])
                start.assert_called_once_with(config)

    def test_here_reconnecting_same_pair_preserves_current_task(self):
        c = '11111111-1111-4111-8111-111111111111'
        x = '22222222-2222-4222-8222-222222222222'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config = {'enabled': True, 'claude': c, 'codex': x, 'repo': str(root),
                      'initiator': 'claude', 'link_id': 'existing'}
            client = MagicMock()
            client.thread.return_value = {'id': x, 'cwd': str(root), 'status': {'type': 'idle'}}
            args = argparse.Namespace(command='here', claude=None, codex=None, repo=None,
                                      initiator='claude', exclude=[], task_file=None, rounds=3, timeout=600)
            with patch.object(relay, 'ROOT', root), patch.object(relay, 'CONFIG', root / 'config.json'), \
                    patch.object(relay, 'discover_sessions', return_value=([session('Claude', c, root), session('Codex', x, root)], [])), \
                    patch.object(relay, 'CodexClient', return_value=client), \
                    patch.object(relay, 'terminal_for_claude', return_value=None), \
                    patch.object(relay, 'start_worker') as start, \
                    patch('sys.stdout', new_callable=io.StringIO):
                relay.write_json(relay.CONFIG, config)
                state = {'epoch': 'human-task', 'prompt': 'Actual task', 'pending': 'review'}
                relay.write_json(relay.state_path(c), state)
                relay.install(args)
                self.assertEqual(relay.read_json(relay.CONFIG), config)
                self.assertEqual(relay.read_json(relay.state_path(c)), state)
                start.assert_not_called()


if __name__ == '__main__':
    unittest.main()
