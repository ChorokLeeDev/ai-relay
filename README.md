# ai-relay

Automatically pass completed Claude Code replies to an existing Codex session for review, then return the review to Claude so it can address valid findings.

The prompt works across repositories. Each handoff includes the selected checkout, current commit, branch, working-tree status, and latest human instruction. Review dialogue stays in the terminal; authorized PR updates should be short, factual English notes.

## Requirements

- Python 3.9 or newer, Git, and a Unix environment. Developed and verified on macOS; native Windows is unsupported.
- Installed, authenticated Claude Code and Codex CLIs.
- Claude Code command hooks and `claude agents --json` support.
- Codex's shared local app-server daemon with its WebSocket control socket. Start Codex normally so the relay can connect to the same daemon.

The script uses only Python's standard library. Existing session permissions apply; the review prompt asks Codex to inspect without changing files or writing to external services.

## Install

~~~bash
git clone https://github.wdf.sap.corp/I767700/ai-relay.git
cd ai-relay
mkdir -p "$HOME/.local/bin"
install -m 755 ai-relay "$HOME/.local/bin/ai-relay"
export PATH="$HOME/.local/bin:$PATH"
~~~

Add that PATH entry to your shell configuration if needed. To update, pull the repository and repeat the `install` command.

## Connect fresh sessions

Open Claude in one terminal and Codex in another, both in the checkout you want to work on:

~~~bash
# First terminal
cd /path/to/repository
claude
~~~

~~~bash
# Second terminal
cd /path/to/repository
codex
~~~

Find the new Claude session's `sessionId` by running `claude agents --json` in another shell. In the Codex terminal, open `/statusline` and enable **Session ID**.

Link the two IDs from a shell:

~~~bash
ai-relay link \
  --claude CLAUDE_SESSION_UUID \
  --codex CODEX_SESSION_UUID \
  --repo /path/to/repository \
  --rounds 3
~~~

Then give Claude the task. The relay sends its completed answer to Codex, waits for that specific review turn to complete, and returns the final review to Claude. It forwards completed answers rather than terminal scrollback or intermediate progress.

`link` registers session-specific handlers in your Claude settings, preserving existing hooks and saving a backup. It connects one pair at a time. A new task in the same pair only needs a new Claude prompt; fresh sessions require linking their new IDs.

## Controls

| Command | Behavior |
| --- | --- |
| `ai-relay status` | Show the linked sessions and recent exchanges. |
| `ai-relay doctor` | Check the daemon connection without sending a model request. |
| `ai-relay off` | Disable forwarding and cancel any pending relay-owned review. |
| `ai-relay retry` | Retry the saved reply after a failed review. |
| `ai-relay retry --request EXCHANGE_ID` | Explicitly retry a saved exchange, including after a newer Claude prompt. |

By default, the relay waits up to 30 minutes for Codex's existing task to finish, then gives the review up to 10 minutes. Configure these separately with `link --wait-timeout SECONDS --timeout SECONDS`.

Each human prompt allows up to three exchanges by default. A reviewer ending with `[AI_RELAY_DONE]` or `[AI_RELAY_USER]` ends the automatic cycle. A request for human approval, interruption, or timeout leaves the review incomplete. A new human prompt supersedes the previous exchange; use an explicit retry if you still want that saved reply reviewed.

Retry workers run detached under `nohup`. A retry's result returns through Claude's next matching Stop hook or next human prompt; it cannot wake an already-idle Claude session on its own.

Settings, backups, prompts, replies, and logs are stored locally in `~/.local/share/ai-relay/`. The relay reads Claude's settings from `~/.claude/settings.json` and Codex's control socket from `$CODEX_HOME/app-server-control/app-server-control.sock`, defaulting to `~/.codex/`. Those local records are not part of this repository.

## Verification

~~~bash
python3 tests/verify_relay.py
~~~

The checks use a mock Unix WebSocket server, synthetic session IDs, and a temporary home directory. They verify message framing, completed-turn correlation, long and fragmented replies, busy-to-idle waiting, round limits, approval handling, saved feedback delivery, and preservation of existing settings. They send no model requests and do not alter your live configuration.

Protocol references: [Codex App Server](https://developers.openai.com/codex/app-server/), [Codex slash commands](https://developers.openai.com/codex/cli/slash-commands/), and [Claude Code hooks](https://code.claude.com/docs/en/hooks).
