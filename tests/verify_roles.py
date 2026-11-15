"""Role announcements, reversed handoffs and decision reports without live agents."""
import importlib.machinery
import importlib.util
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

loader = importlib.machinery.SourceFileLoader(
    'ai_relay_roles', str(Path(__file__).resolve().parents[1] / 'ai-relay'))
spec = importlib.util.spec_from_loader(loader.name, loader)
relay = importlib.util.module_from_spec(spec)
loader.exec_module(relay)
CLAUDE = '11111111-1111-4111-8111-111111111111'
CODEX = '22222222-2222-4222-8222-222222222222'


def turn(tid, prompt='Review the change', reply='My analysis', status='completed'):
    return {'id': tid, 'status': status, 'items': [
        {'type': 'userMessage', 'content': [{'type': 'text', 'text': prompt}]},
        {'type': 'agentMessage', 'phase': 'commentary', 'text': 'Progress only'},
        {'type': 'agentMessage', 'phase': 'final_answer', 'text': reply},
    ]}


class RoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [patch.object(relay, 'ROOT', self.root),
                        patch.object(relay, 'CONFIG', self.root / 'config.json')]
        for p in self.patches:
            p.start()
        self.config = {'enabled': True, 'claude': CLAUDE, 'codex': CODEX,
                       'repo': str(self.root), 'link_id': 'test-link', 'initiator': 'codex',
                       'rounds': 2, 'timeout': 10, 'wait_timeout': 10}
        relay.write_json(relay.CONFIG, self.config)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_startup_asks_who_leads_and_supports_explicit_choice(self):
        with patch.object(relay.sys.stdin, 'isatty', return_value=True), \
                patch('builtins.input', side_effect=['invalid', '2']), \
                patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(relay.choose_initiator(), 'codex')
        with patch.object(relay.sys.stdin, 'isatty', return_value=False):
            self.assertEqual(relay.choose_initiator(), 'claude')
        with patch('builtins.input') as ask:
            self.assertEqual(relay.choose_initiator('codex'), 'codex')
            ask.assert_not_called()

    def test_decision_panel_does_not_treat_an_incidental_tldr_reference_as_a_decision(self):
        self.assertEqual(relay.decision_summary('I will report with TL;DR next time.\nWaiting.'), '')
        summary = relay.decision_summary('**TL;DR — Decision**\nDecided: Use this approach.\n[AI_RELAY_DONE]')
        self.assertIn('Decided: Use this approach.', summary)
        self.assertNotIn('AI_RELAY_DONE', summary)

    def test_both_roles_receive_automatic_context_without_new_authorization(self):
        for agent, role in [('codex', 'initiator'), ('claude', 'reviewer')]:
            context = relay.role_context(self.config, agent)
            self.assertIn('You are the ' + role, context)
            self.assertIn('TL;DR', context)
            self.assertIn('no new authorization', context)
            self.assertNotIn('PR #', context)
        start = relay.hook({'session_id': CLAUDE, 'hook_event_name': 'SessionStart'})
        self.assertIn('reviewer', start['hookSpecificOutput']['additionalContext'])
        self.assertEqual(relay.hook({'session_id': CODEX, 'hook_event_name': 'SessionStart'}), {})

    def test_waiting_summary_is_not_reviewed_until_background_research_finishes(self):
        self.config['initiator'] = 'claude'
        relay.write_json(relay.CONFIG, self.config)
        relay.write_json(relay.state_path(CLAUDE), {'epoch': 'research', 'round': 0,
                                                 'prompt': 'Find a verified position.', 'done': False})
        for status in ('running', 'pending', 'queued'):
            with self.subTest(status=status), patch.object(relay, 'CodexClient') as client:
                result = relay.hook({'session_id': CLAUDE, 'hook_event_name': 'Stop',
                                     'last_assistant_message': 'Waiting for the researcher.',
                                     'background_tasks': [{'type': 'agent', 'status': status}]})
                self.assertEqual(result, {})
                client.assert_not_called()
                self.assertEqual(relay.read_json(relay.state_path(CLAUDE))['round'], 0)

    def test_concluded_claude_reply_yields_without_another_stop_hook_review(self):
        self.config['initiator'] = 'claude'
        relay.write_json(relay.CONFIG, self.config)
        for marker in ('[AI_RELAY_DONE]', '[AI_RELAY_USER]'):
            relay.write_json(relay.state_path(CLAUDE), {'epoch': 'reviewed', 'round': 1, 'done': False})
            with self.subTest(marker=marker), patch.object(relay, 'CodexClient') as client:
                result = relay.hook({'session_id': CLAUDE, 'hook_event_name': 'Stop',
                                     'last_assistant_message': 'Here is the final conclusion. ' + marker})
                self.assertEqual(result, {})
                client.assert_not_called()
                self.assertTrue(relay.read_json(relay.state_path(CLAUDE))['done'])

    def test_background_notification_keeps_human_instructions_and_queued_task(self):
        self.config['initiator'] = 'claude'
        relay.write_json(relay.CONFIG, self.config)
        state = {'epoch': 'human-task', 'round': 0, 'prompt': 'Verify both fit criteria.', 'done': False}
        relay.write_json(relay.state_path(CLAUDE), state)
        task_id = relay.queue_task(self.config, 'Show the verified output here.', start=False)
        relay.hook({'session_id': CLAUDE, 'hook_event_name': 'UserPromptSubmit',
                    'prompt': '<task-notification>Background researcher finished.</task-notification>'})
        self.assertEqual(relay.read_json(relay.state_path(CLAUDE)), state)
        with patch.object(relay, 'send_claude') as send:
            relay.task_worker(task_id)
        send.assert_called_once()
        self.assertTrue(send.call_args.args[2]())

    def test_setup_does_not_reset_task_or_trigger_peer_review(self):
        path = relay.state_path(CLAUDE)
        relay.write_json(path, {'epoch': 'original', 'round': 1, 'prompt': 'Real task', 'done': False})
        prompt = relay.hook({'session_id': CLAUDE, 'hook_event_name': 'UserPromptSubmit',
                             'prompt': '[AI_RELAY_SETUP test-link] Know your role'})
        self.assertIn('reviewer', prompt['hookSpecificOutput']['additionalContext'])
        self.assertEqual(relay.read_json(path)['epoch'], 'original')
        with patch.object(relay, 'CodexClient') as client:
            self.assertEqual(relay.hook({'session_id': CLAUDE, 'hook_event_name': 'Stop',
                                         'last_assistant_message': 'Understood'}), {})
            client.assert_not_called()
        self.assertEqual(relay.read_json(path)['prompt'], 'Real task')

    def test_codex_to_claude_to_codex_correlates_one_review_and_reports_decision(self):
        origin = turn('human-task')
        client = MagicMock()
        decision = '**TL;DR — Decision**\n- Decided: Fix the contract first.\n- Why: Validation fails.\n- Next: Verify one turn.\n\n[AI_RELAY_DONE]'
        client.review.return_value = (decision, 'feedback-turn')

        def deliver(config, prompt, current):
            self.assertTrue(current())
            relay.hook({'session_id': CLAUDE, 'hook_event_name': 'UserPromptSubmit', 'prompt': prompt})
            relay.hook({'session_id': CLAUDE, 'hook_event_name': 'Stop',
                        'last_assistant_message': 'Fix the contract. [AI_RELAY_DONE]'})

        with patch.object(relay, 'thread_turns', return_value=[origin]), \
                patch.object(relay, 'send_claude', side_effect=deliver):
            relay.codex_cycle(client, self.config, origin)
        client.review.assert_called_once()
        sent = client.review.call_args.args[1]
        self.assertTrue(sent.startswith('[AI_RELAY_FEEDBACK '))
        self.assertIn('Fix the contract.', sent)
        self.assertIn('You are the initiator', sent)
        state = relay.read_json(relay.state_path(CLAUDE))
        self.assertTrue(state['done'])
        self.assertEqual(state['status'], 'review_complete')
        self.assertEqual(state['turn'], 'feedback-turn')
        summary = relay.read_json(self.root / 'decision.json')['text']
        self.assertIn('Decided: Fix the contract', summary)
        self.assertNotIn('Progress only', summary)
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            relay.show_decision(self.config)
        self.assertIn('╔═ TL;DR', output.getvalue())

    def test_new_human_task_supersedes_old_review_without_sending_feedback(self):
        origin = turn('first-task')
        client = MagicMock()
        with patch.object(relay, 'thread_turns', return_value=[origin, turn('new-task')]), \
                patch.object(relay, 'send_claude') as send:
            relay.codex_cycle(client, self.config, origin)
        send.assert_not_called()
        client.review.assert_not_called()
        self.assertEqual(relay.read_json(relay.state_path(CLAUDE))['status'], 'superseded')

    def test_watcher_skips_history_and_relay_turns(self):
        old = turn('old')
        automatic = turn('relay', '[AI_RELAY_FEEDBACK request] Peer reply')
        with patch.object(relay, 'link_current', side_effect=[True, False]), \
                patch.object(relay, 'thread_turns', return_value=[old, automatic]), \
                patch.object(relay, 'codex_cycle') as cycle, patch.object(relay.time, 'sleep'):
            relay.watch_codex(MagicMock(), self.config, {'old'})
        cycle.assert_not_called()

    def test_watcher_forwards_new_completed_human_turn_only(self):
        origin = turn('new')
        with patch.object(relay, 'link_current', side_effect=[True, False]), \
                patch.object(relay, 'thread_turns', return_value=[origin]), \
                patch.object(relay, 'codex_cycle') as cycle, patch.object(relay.time, 'sleep'):
            client = MagicMock()
            relay.watch_codex(client, self.config, set())
        cycle.assert_called_once_with(client, self.config, origin)

    def test_terminal_delivery_preserves_draft_and_permission_prompts(self):
        self.assertTrue(relay.empty_claude_prompt('❯ \n────────────────\nstatus'))
        for screen in ['❯ My draft\n────', '❯ \nsecond input line\n────', 'Allow tool?\n1. Allow']:
            self.assertFalse(relay.empty_claude_prompt(screen))
        config = {**self.config, 'claude_terminal': {'transport': 'iterm', 'tty': '/dev/test', 'pid': 7}}
        agent = {'id': CLAUDE, 'pid': 7, 'status': 'idle'}
        with patch.object(relay, 'discover_claude', return_value=[agent]), \
                patch.object(relay, 'terminal_screen', return_value='❯ Draft\n────'), \
                patch.object(relay.subprocess, 'run') as send:
            with self.assertRaisesRegex(TimeoutError, 'no text was inserted'):
                relay.send_claude(config, 'Peer review', lambda: True, timeout=0)
            send.assert_not_called()

    def test_literal_peer_text_is_passed_as_data_to_terminal(self):
        config = {**self.config, 'claude_terminal': {'transport': 'iterm', 'tty': '/dev/test', 'pid': 7}}
        literal = 'Literal `commands` and $(text) remain literal'
        def receive_file(command, **kwargs):
            self.assertEqual(Path(command[-1]).read_text(), literal)
            self.assertNotIn('shell', kwargs)
            return subprocess.CompletedProcess([], 0, 'sent', '')
        with patch.object(relay, 'discover_claude', return_value=[{'id': CLAUDE, 'pid': 7, 'status': 'idle'}]), \
                patch.object(relay, 'terminal_screen', return_value='❯ \n────'), \
                patch.object(relay.subprocess, 'run', side_effect=receive_file) as run:
            relay.send_claude(config, literal, lambda: True)
        self.assertFalse(Path(run.call_args.args[0][-1]).exists())
        self.assertNotIn('shell', run.call_args.kwargs)

    def test_busy_background_wait_accepts_task_at_empty_prompt(self):
        config = {**self.config, 'claude_terminal': {'transport': 'iterm', 'tty': '/dev/test', 'pid': 7}}
        screen = '✻ Waiting for 1 background agent to finish\n108653 tokens\n────\n❯ \n────\nstatus'
        with patch.object(relay, 'discover_claude', return_value=[{'id': CLAUDE, 'pid': 7, 'status': 'busy'}]), \
                patch.object(relay, 'terminal_screen', return_value=screen), \
                patch.object(relay.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'sent', '')) as send:
            relay.send_claude(config, 'Show the search results in this terminal.', lambda: True, timeout=0)
        send.assert_called_once()

    def test_background_wait_does_not_override_draft_active_work_or_permission(self):
        config = {**self.config, 'claude_terminal': {'transport': 'iterm', 'tty': '/dev/test', 'pid': 7}}
        screens = [
            '✻ Waiting for 2 background agents to finish\n────\n❯ My draft\n────',
            '✻ Waiting for 1 background agent to finish\n✢ Computing…\n────\n❯ \n────',
            '✻ Waiting for 1 background agent to finish\nAllow tool?\n1. Allow\n────\n❯ \n────',
            '✢ Computing…\n────\n❯ \n────',
        ]
        for screen in screens:
            with self.subTest(screen=screen), \
                    patch.object(relay, 'discover_claude', return_value=[{'id': CLAUDE, 'pid': 7, 'status': 'busy'}]), \
                    patch.object(relay, 'terminal_screen', return_value=screen), \
                    patch.object(relay.subprocess, 'run') as send:
                with self.assertRaises(TimeoutError):
                    relay.send_claude(config, 'Search results', lambda: True, timeout=0)
                send.assert_not_called()

    def test_claude_task_worker_starts_before_reviewer_role_worker(self):
        config = {**self.config, 'initiator': 'claude', 'task_id': 'queued-task'}
        def start_roles(*args, **kwargs):
            task.assert_called_once_with('queued-task')
            self.assertEqual(args[0][:2], ['nohup', relay.sys.executable])
            return MagicMock(pid=99)
        with patch.object(relay, 'start_task_worker') as task, \
                patch.object(relay.subprocess, 'Popen', side_effect=start_roles) as roles:
            relay.start_worker(config)
        roles.assert_called_once()

    def test_claude_startup_does_not_deliver_the_task_twice_or_paste_setup_over_it(self):
        self.config.update({'initiator': 'claude', 'task_id': 'independent-task',
                            'claude_terminal': {'transport': 'iterm', 'tty': '/dev/test', 'pid': 7}})
        relay.write_json(relay.CONFIG, self.config)
        with patch.object(relay, 'CodexClient', return_value=MagicMock()), \
                patch.object(relay, 'thread_turns', return_value=[]), \
                patch.object(relay, 'send_claude') as send, patch.object(relay, 'task_worker') as task:
            relay.startup_worker('test-link')
        send.assert_not_called()
        task.assert_not_called()

    def test_startup_announces_both_roles_then_watches_codex(self):
        self.config['claude_terminal'] = {'transport': 'iterm', 'tty': '/dev/test', 'pid': 7}
        relay.write_json(relay.CONFIG, self.config)
        client = MagicMock()
        client.review.return_value = ('Ready', 'setup-turn')
        with patch.object(relay, 'CodexClient', return_value=client), \
                patch.object(relay, 'thread_turns', return_value=[turn('old')]), \
                patch.object(relay, 'send_claude') as send, \
                patch.object(relay, 'watch_codex') as watch:
            relay.startup_worker('test-link')
        self.assertIn('[AI_RELAY_SETUP test-link]', send.call_args.args[1])
        self.assertIn('You are the reviewer', send.call_args.args[1])
        self.assertIn('You are the initiator', client.review.call_args.args[1])
        watch.assert_called_once_with(client, self.config, {'old'})
        client.close.assert_called_once()

    def test_multiline_context_is_delivered_as_one_message_file(self):
        config = {**self.config, 'claude_terminal': {'transport': 'iterm', 'tty': '/dev/test', 'pid': 7}}
        prompt = '[AI_RELAY_SETUP test-link]\n' + relay.role_context(config, 'claude')
        def receive_file(command, **kwargs):
            self.assertEqual(Path(command[-1]).read_text(), prompt)
            return subprocess.CompletedProcess([], 0, 'sent', '')
        with patch.object(relay, 'discover_claude', return_value=[{'id': CLAUDE, 'pid': 7, 'status': 'idle'}]), \
                patch.object(relay, 'terminal_screen', return_value='❯ \n────'), \
                patch.object(relay.subprocess, 'run', side_effect=receive_file):
            relay.send_claude(config, prompt, lambda: True)

    def test_queued_task_delivers_to_participating_claude_and_cancels_on_new_human_prompt(self):
        self.config['initiator'] = 'claude'
        relay.write_json(relay.CONFIG, self.config)
        relay.write_json(relay.state_path(CLAUDE), {'epoch': 'task-epoch'})
        task_id = relay.queue_task(self.config, 'Read the PDF and prepare next steps.', start=False)
        with patch.object(relay, 'send_claude') as send:
            relay.task_worker(task_id)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[1], 'Read the PDF and prepare next steps.')
        self.assertTrue(send.call_args.args[2]())
        relay.write_json(relay.state_path(CLAUDE), {'epoch': 'new-human-task'})
        with patch.object(relay, 'send_claude') as send:
            relay.task_worker(task_id)
        send.assert_not_called()

    def test_queued_task_cannot_follow_a_changed_link(self):
        relay.write_json(relay.state_path(CLAUDE), {'epoch': 'task-epoch'})
        task_id = relay.queue_task(self.config, 'Original task', start=False)
        relay.write_json(relay.CONFIG, {**self.config, 'link_id': 'replacement'})
        with patch.object(relay, 'send_claude') as send, patch.object(relay, 'CodexClient') as client:
            relay.task_worker(task_id)
        send.assert_not_called()
        client.assert_not_called()

    def test_queued_codex_initiator_task_uses_its_existing_thread(self):
        relay.write_json(relay.state_path(CLAUDE), {'epoch': 'task-epoch'})
        task_id = relay.queue_task(self.config, 'Prepare the next interview.', start=False)
        client = MagicMock()
        def review(thread, prompt, **kwargs):
            kwargs['on_start']('task-turn')
            # The watcher may notice the completed origin before the worker receives its final event.
            relay.write_json(relay.state_path(CLAUDE), {'epoch': 'watcher-review'})
            self.assertTrue(factory.call_args.args[1]())
            return '**TL;DR — Decision**\nDecided: Prepare the brief.', 'task-turn'
        client.review.side_effect = review
        with patch.object(relay, 'CodexClient', return_value=client) as factory, \
                patch.object(relay, 'send_claude') as send:
            relay.task_worker(task_id)
        client.review.assert_called_once()
        self.assertEqual(client.review.call_args.args, (CODEX, 'Prepare the next interview.'))
        send.assert_not_called()
        client.close.assert_called_once()

    def test_busy_claude_gets_hook_context_while_codex_notice_still_runs(self):
        self.config['claude_terminal'] = {'transport': 'iterm', 'tty': '/dev/test', 'pid': 7}
        relay.write_json(relay.CONFIG, self.config)
        client = MagicMock()
        with patch.object(relay, 'CodexClient', return_value=client), \
                patch.object(relay, 'thread_turns', return_value=[]), \
                patch.object(relay, 'send_claude', side_effect=TimeoutError('busy')), \
                patch.object(relay, 'watch_codex'):
            relay.startup_worker('test-link')
        client.review.assert_called_once()
        context = relay.hook({'session_id': CLAUDE, 'hook_event_name': 'PreToolUse', 'tool_name': 'Read'})
        self.assertIn('You are the reviewer', context['hookSpecificOutput']['additionalContext'])


if __name__ == '__main__':
    unittest.main()
