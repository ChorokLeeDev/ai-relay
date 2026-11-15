import argparse
import base64
import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
from unittest.mock import patch

loader = importlib.machinery.SourceFileLoader('ai_relay', str(Path(__file__).resolve().parents[1] / 'ai-relay'))
spec = importlib.util.spec_from_loader(loader.name, loader)
relay = importlib.util.module_from_spec(spec)
loader.exec_module(relay)
folder = Path(tempfile.mkdtemp(prefix='relay-test-', dir='/tmp'))
relay.ROOT = folder / 'state'
relay.CONFIG = relay.ROOT / 'config.json'
home = folder / 'home'
socket_path = home / '.codex/app-server-control/app-server-control.sock'
socket_path.parent.mkdir(parents=True)
os.environ['HOME'] = str(home)
os.environ['CODEX_HOME'] = str(home / '.codex')
claude = '11111111-1111-4111-8111-111111111111'
codex = '22222222-2222-4222-8222-222222222222'
mode = 'feedback'
calls = []
errors = []
listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
listener.bind(str(socket_path))
listener.listen()
listener.settimeout(0.2)
stopping = threading.Event()


def exact(connection, length):
    result = b''
    while len(result) < length:
        chunk = connection.recv(length - len(result))
        if not chunk:
            raise EOFError
        result += chunk
    return result


def receive(connection):
    flags, length = exact(connection, 2)
    masked = length & 128
    length &= 127
    if length == 126:
        length = struct.unpack('!H', exact(connection, 2))[0]
    elif length == 127:
        length = struct.unpack('!Q', exact(connection, 8))[0]
    assert masked, 'Client WebSocket frames must be masked'
    mask = exact(connection, 4)
    payload = exact(connection, length)
    payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
    if flags & 15 == 10:
        return receive(connection)
    return json.loads(payload)


def frame(connection, data, opcode=1, final=True):
    header = bytes([(128 if final else 0) | opcode])
    if len(data) < 126:
        header += bytes([len(data)])
    elif len(data) < 65536:
        header += bytes([126]) + struct.pack('!H', len(data))
    else:
        header += bytes([127]) + struct.pack('!Q', len(data))
    connection.sendall(header + data)


def send(connection, message, fragmented=False):
    payload = json.dumps(message, ensure_ascii=False).encode()
    if fragmented:
        frame(connection, payload[:20], final=False)
        frame(connection, b'ping', opcode=9)
        frame(connection, payload[20:], opcode=0)
    else:
        frame(connection, payload)


def serve():
    while not stopping.is_set():
        try:
            connection, _ = listener.accept()
        except socket.timeout:
            continue
        with connection:
            try:
                silent_turn = None
                header = b''
                while b'\r\n\r\n' not in header:
                    header += connection.recv(4096)
                key = [line.split(':', 1)[1].strip() for line in header.decode().split('\r\n')
                       if line.lower().startswith('sec-websocket-key:')][0]
                accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
                connection.sendall(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                                    + 'Sec-WebSocket-Accept: ' + accept + '\r\n\r\n').encode())
                while True:
                    message = receive(connection)
                    method = message['method']
                    calls.append(message)
                    if 'id' not in message:
                        continue
                    if method == 'initialize':
                        send(connection, {'id': message['id'], 'result': {}})
                    elif method in ('thread/read', 'thread/resume'):
                        if mode == 'fresh' and method == 'thread/resume':
                            send(connection, {'id': message['id'], 'error': {'message': 'no rollout found for thread id'}})
                            continue
                        turns = []
                        if mode == 'fresh' and silent_turn and message['params'].get('includeTurns'):
                            turns = [{'id': 'unrelated', 'status': 'completed', 'items': [
                                {'type': 'agentMessage', 'text': 'Wrong turn'}]},
                                {'id': silent_turn, 'status': 'completed', 'items': [
                                    {'type': 'agentMessage', 'phase': 'commentary', 'text': 'Progress'},
                                    {'type': 'agentMessage', 'phase': 'final_answer', 'text': 'Fresh session ready'}]}]
                        send(connection, {'id': message['id'], 'result': {'thread': {
                            'id': codex, 'cwd': '/test/project',
                            'status': {'type': 'active' if mode == 'busy' else 'idle'}, 'turns': turns}}})
                    elif method == 'turn/start':
                        turn = 'test-turn-' + str(len(calls))
                        send(connection, {'method': 'turn/completed', 'params': {
                            'threadId': 'unrelated-thread', 'turn': {'id': 'other', 'status': 'completed'}}})
                        send(connection, {'id': message['id'], 'result': {'turn': {'id': turn}}})
                        if mode == 'fresh':
                            silent_turn = turn
                            continue
                        if mode == 'approval':
                            send(connection, {'id': 'server-approval', 'method': 'item/commandExecution/requestApproval',
                                              'params': {'threadId': codex, 'turnId': turn}})
                            continue
                        if mode == 'wait':
                            continue
                        for phase, text in [('commentary', 'OLD COMMENTARY SHOULD NOT BE FORWARDED'),
                                            ('final_answer', '현재 커밋 검토 결과: ' + ('긴 응답 ' * 14000)
                                             + ('\n[AI_RELAY_DONE]' if mode == 'done' else '\nFix the assertion.'))]:
                            send(connection, {'method': 'item/completed', 'params': {
                                'threadId': codex, 'turnId': turn,
                                'item': {'id': phase, 'type': 'agentMessage', 'phase': phase, 'text': text}}},
                                fragmented=phase == 'final_answer')
                        send(connection, {'method': 'turn/completed', 'params': {
                            'threadId': codex, 'turn': {'id': turn, 'status': 'completed', 'items': []}}})
                    elif method == 'turn/interrupt':
                        send(connection, {'id': message['id'], 'result': {}})
                    else:
                        raise AssertionError('Unexpected RPC method: ' + method)
            except (EOFError, BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:
                errors.append(repr(exc))


thread = threading.Thread(target=serve, daemon=True)
thread.start()
try:
    config = {'enabled': True, 'claude': claude, 'codex': codex, 'repo': str(folder), 'rounds': 2, 'timeout': 10}
    relay.write_json(relay.CONFIG, config)
    event = {'session_id': claude, 'hook_event_name': 'UserPromptSubmit', 'prompt': '검토해. 기존 승인은 유지해.'}
    assert relay.hook({**event, 'session_id': '33333333-3333-4333-8333-333333333333'}) == {}
    assert not calls
    relay.hook(event)
    stop = {'session_id': claude, 'hook_event_name': 'Stop', 'last_assistant_message': 'Literal ' + chr(96) + 'text' + chr(96) + ' $(keep literal)\n전체 테스트 통과'}
    epoch = relay.read_json(relay.state_path(claude))['epoch']
    assert relay.hook({**stop, 'background_tasks': [{'type': 'agent', 'status': 'running'}]}) == {}
    assert not any(call['method'] == 'turn/start' for call in calls)
    relay.hook({**event, 'prompt': '<task-notification>Research finished.</task-notification>'})
    assert relay.read_json(relay.state_path(claude))['epoch'] == epoch
    first = relay.hook({**stop, 'background_tasks': [{'type': 'agent', 'status': 'completed'}]})
    context = first['hookSpecificOutput']['additionalContext']
    assert '현재 커밋 검토 결과' in context
    assert 'OLD COMMENTARY' not in context
    assert 'another review will follow' in context.lower()
    start = [call for call in calls if call['method'] == 'turn/start'][-1]
    assert set(start['params']) == {'threadId', 'input'}
    assert stop['last_assistant_message'] in start['params']['input'][0]['text']
    assert 'CHECKOUT AT HANDOFF' in start['params']['input'][0]['text']
    assert event['prompt'] in start['params']['input'][0]['text']
    second = relay.hook({**stop, 'stop_hook_active': True})
    assert 'final automatic exchange' in second['hookSpecificOutput']['additionalContext'].lower()
    count = len([call for call in calls if call['method'] == 'turn/start'])
    assert relay.hook(stop) == {}
    assert len([call for call in calls if call['method'] == 'turn/start']) == count
    mode = 'done'
    relay.hook(event)
    assert '[AI_RELAY_DONE]' in relay.hook(stop)['hookSpecificOutput']['additionalContext']
    assert relay.hook(stop) == {}
    mode = 'busy'
    relay.hook(event)
    count = len([call for call in calls if call['method'] == 'turn/start'])
    def finish_other_task():
        global mode
        time.sleep(0.2)
        mode = 'feedback'
    finish = threading.Thread(target=finish_other_task)
    finish.start()
    assert '현재 커밋 검토 결과' in relay.hook(stop)['hookSpecificOutput']['additionalContext']
    finish.join()
    assert len([call for call in calls if call['method'] == 'turn/start']) == count + 1
    events = [json.loads(line) for line in (relay.ROOT / 'events.jsonl').read_text().splitlines()]
    assert any(item['event'] == 'waiting_for_codex' for item in events)
    assert not any(call['method'] == 'turn/interrupt' for call in calls)
    mode = 'fresh'
    client = relay.CodexClient(timeout=10)
    try:
        client.initialize()
        reply, fresh_turn = client.review(codex, '[AI_RELAY_SETUP test] Acknowledge only.')
        assert reply == 'Fresh session ready'
        assert client.finished
    finally:
        client.close()
    assert not any(call['method'] == 'turn/interrupt' for call in calls)
    mode = 'approval'
    relay.hook(event)
    assert 'human approval or input' in relay.hook(stop)['hookSpecificOutput']['additionalContext']
    time.sleep(0.1)
    assert any(call['method'] == 'turn/interrupt' for call in calls)
    mode = 'wait'
    relay.hook(event)
    def disable():
        time.sleep(0.1)
        relay.write_json(relay.CONFIG, {**config, 'enabled': False})
    cancel = threading.Thread(target=disable)
    cancel.start()
    assert relay.hook(stop) == {}
    cancel.join()
    relay.write_json(relay.CONFIG, config)
    state = relay.read_json(relay.state_path(claude))
    relay.write_json(relay.ROOT / 'feedback.json', {'session': claude, 'epoch': state['epoch'],
        'request': 'saved-review', 'text': 'Saved completed Codex review'})
    delivered = relay.hook(event)
    assert delivered['hookSpecificOutput']['hookEventName'] == 'UserPromptSubmit'
    assert delivered['hookSpecificOutput']['additionalContext'].startswith('Saved completed Codex review')
    assert not (relay.ROOT / 'feedback.json').exists()
    state = relay.read_json(relay.state_path(claude))
    state['feedback_pending'] = True
    relay.write_json(relay.state_path(claude), state)
    count = len([call for call in calls if call['method'] == 'turn/start'])
    def deliver_retry():
        time.sleep(0.1)
        relay.write_json(relay.ROOT / 'feedback.json', {'session': claude, 'epoch': state['epoch'],
            'request': 'retry-review', 'text': 'Retried review ready for Claude'})
    delivery = threading.Thread(target=deliver_retry)
    delivery.start()
    assert relay.hook(stop)['hookSpecificOutput']['additionalContext'].startswith('Retried review ready for Claude')
    delivery.join()
    assert len([call for call in calls if call['method'] == 'turn/start']) == count
    assert 'feedback_pending' not in relay.read_json(relay.state_path(claude))
    mode = 'feedback'
    settings = home / '.claude/settings.json'
    preserved = {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': '/existing/status'}]}]}, 'custom': 'preserve me'}
    relay.write_json(settings, preserved)
    arguments = argparse.Namespace(claude=claude, codex=codex, repo=str(folder), rounds=2, timeout=10)
    with patch.object(relay, 'start_worker'), patch.object(relay, 'terminal_for_claude', return_value=None):
        relay.install(arguments)
        relay.install(arguments)
    installed = relay.read_json(settings)
    assert installed['custom'] == preserved['custom']
    assert installed['hooks']['Stop'][0] == preserved['hooks']['Stop'][0]
    assert len(installed['hooks']['Stop']) == 2
    assert len(installed['hooks']['UserPromptSubmit']) == 1
    assert len(installed['hooks']['SessionStart']) == 1
    # Startup and Codex initiation reuse a client: a later approval must still
    # interrupt only its new relay-owned turn, even after an earlier completed turn.
    mode = 'feedback'
    client = relay.CodexClient(timeout=10)
    client.initialize()
    client.review(codex, 'First setup turn')
    mode = 'approval'
    interrupts = len([call for call in calls if call['method'] == 'turn/interrupt'])
    try:
        client.review(codex, 'Later relay turn')
        raise AssertionError('Approval request should stop the later review')
    except RuntimeError as exc:
        assert 'human approval' in str(exc)
    finally:
        client.close()
    time.sleep(0.1)
    assert len([call for call in calls if call['method'] == 'turn/interrupt']) == interrupts + 1
    assert not errors, errors
    print('PASS: existing transport checks, busy-to-idle waiting without interruption, review started once after idle, final-output-only forwarding, round limits, saved feedback delivery on next Claude prompt, human-approval stop, disabling, preserved settings.', flush=True)
    print('Artifacts:', folder, flush=True)
finally:
    stopping.set()
    thread.join(timeout=2)
    listener.close()
