# ai-relay

Connect existing Claude Code and Codex terminals, choose who leads, and automatically exchange completed replies for peer review.

The prompt works across repositories. Each handoff includes the selected checkout, current commit, branch, working-tree status, and latest human instruction. Review dialogue stays in the terminal; authorized PR updates should be short, factual English notes.

## Requirements

- Python 3.9 or newer, Git, and a Unix environment. Developed and verified on macOS; native Windows is unsupported.
- Installed, authenticated Claude Code and Codex CLIs.
- Claude Code command hooks and `claude agents --json` support.
- Codex's shared local app-server daemon with its WebSocket control socket. Start Codex normally so the relay can connect to the same daemon.
- For **Codex initiation**, the Claude terminal must be in iTerm2 on macOS or a tmux pane. Claude initiation also works through Claude hooks without a supported terminal bridge.

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

From another shell in the same repository:

~~~bash
ai-relay sessions
ai-relay link
~~~

`sessions` detects running interactive Claude processes and Codex sessions loaded in the local daemon. It shows their IDs, directories, status, and names. Fresh Codex terminals are ready to connect without typing an initial message. Discovery sends no model requests; the startup announcement initializes the selected conversation when needed.

`link` automatically selects a running session for each agent in the current workspace, including subdirectories of a Git checkout. Workspaces without Git are also supported. If several match, it asks you to select from a numbered list. When none match, an interactive shell lets you choose from sessions in other directories. Scripts must provide IDs for ambiguous selections.

To connect the terminals you just opened from any directory, run:

~~~bash
ai-relay here
~~~

`here` detects Claude and Codex in a shared workspace. It prefers a new pair over the previous link, excludes the calling Codex session using `CODEX_THREAD_ID`, and asks for selection when multiple pairs remain. You can narrow selection with `--repo` or `--claude/--codex`. Reconnecting the same pair preserves its current task.

To connect and send a task into the participating initiator terminal in one step:

~~~bash
ai-relay here --initiator claude --task-file /path/to/task.txt
~~~

The file contains the actual task, with normal UTF-8 text and newlines. Delivery waits for busy sessions and preserves unsent input. Claude receives a bracketed paste in its iTerm2 or tmux terminal; Codex receives a new turn in its existing terminal conversation. Claude task delivery runs independently of Codex's startup acknowledgment. An empty Claude prompt showing “Waiting for … background agent(s) to finish” can receive the task even when session discovery reports busy; active work and permission dialogs still wait. A queued task is canceled if the link changes or a newer human Claude prompt supersedes it. Background completion notifications preserve the human task and its queued work. Role announcements alone do not start the task.

For requests from a separate Codex conversation, install the included skill so “connect the terminals I opened and do this there” uses the same flow:

~~~bash
mkdir -p "$HOME/.codex/skills/ai-relay"
cp skills/ai-relay/SKILL.md "$HOME/.codex/skills/ai-relay/SKILL.md"
~~~

The skill is available in new Codex sessions after installation. It routes requests involving the relay to `here` and verifies actual delivery; it does not redirect unrelated tasks.

At startup, `link` asks who leads:

~~~text
AI RELAY — who leads this session?
  1. Claude initiates; Codex reviews (default)
  2. Codex initiates; Claude reviews
~~~

Give the task to the selected initiator. The other session reviews its completed answer and the initiator evaluates that feedback. For scripts or a preset role, use `ai-relay link --initiator codex` or `--initiator claude`; noninteractive invocations default to Claude.

Both agents receive their role and relay instructions automatically. The relay queues a short startup acknowledgment in Codex. It also sends one to Claude when its supported terminal is idle with an empty prompt; otherwise Claude receives the same context through its next session-start, human-prompt, or tool hook. You do not need to explain the relay for each task. Startup acknowledgments are excluded from review cycles. These announcements make small model requests; session discovery and `doctor` do not.

For a separate review checkout, use `ai-relay link --repo /path/to/checkout`. You can still specify either or both session IDs:

~~~bash
ai-relay link \
  --claude CLAUDE_SESSION_UUID \
  --codex CODEX_SESSION_UUID \
  --repo /path/to/repository \
  --rounds 3
~~~

The connection is checked for a live, input-capable Codex thread before settings are changed. It forwards completed answers rather than terminal scrollback or intermediate progress. In Codex mode a detached watcher observes new human tasks, delivers a review prompt to the selected Claude terminal, and returns its matching review to Codex. It skips historical tasks, setup turns, and relay-generated turns. It waits for busy sessions and preserves unsent Claude input.

`link` registers session-specific handlers in your Claude settings, preserving existing hooks and saving a backup. It connects one pair at a time. A new task in the same pair only needs a new initiator prompt; fresh sessions require linking again.

Routine reviewer disagreements should end in a decision within the existing task scope. The initiator is instructed to lead decision reports with **TL;DR — Decision**, stating what was decided, why, and the next step. Necessary missing facts and approvals still require user input. The latest formatted decision is also available with `ai-relay decisions`; it contains the initiator's summary, not the peer conversation.

## Controls

| Command | Behavior |
| --- | --- |
| `ai-relay sessions` | Detect Claude processes and loaded Codex sessions. |
| `ai-relay sessions --json` | Return detected sessions and discovery problems for scripts. |
| `ai-relay link` | Detect and connect sessions for the current checkout. |
| `ai-relay here [--task-file PATH]` | Detect a new pair across workspaces and optionally deliver the task. |
| `ai-relay status` | Show the linked sessions and recent exchanges. |
| `ai-relay decisions` | Show the latest initiator decision in a prominent TL;DR panel. |
| `ai-relay doctor` | Check daemon connectivity and session resumability without sending a model request. |
| `ai-relay off` | Disable forwarding and cancel any pending relay-owned review. |
| `ai-relay retry` | Retry the saved reply after a failed Claude-initiated review. |
| `ai-relay retry --request EXCHANGE_ID` | Explicitly retry a saved exchange, including after a newer Claude prompt. |

By default, the relay waits up to 30 minutes for Codex's existing task to finish, then gives the review up to 10 minutes. Configure these separately with `link --wait-timeout SECONDS --timeout SECONDS`.

Each human prompt allows up to three exchanges by default. A reviewer's `[AI_RELAY_DONE]` or `[AI_RELAY_USER]` ends the automatic cycle. Claude's own end markers stop further reviews only after at least one peer review has completed for the current human task; markers on its first proposal still hand that proposal to Codex. Waiting for peer feedback is not missing user input. Waiting summaries with live background tasks are not sent for peer review; the completed result is reviewed when the background work finishes. A request for human approval, interruption, or timeout leaves the review incomplete. A new human prompt supersedes the previous exchange; use an explicit retry if you still want that saved reply reviewed.

Startup, watcher, and retry workers run detached under `nohup`. A retry's result returns through Claude's next matching Stop hook or next human prompt; it cannot wake an already-idle Claude session on its own. The Codex-mode terminal bridge handles waking its Claude reviewer.

Settings, backups, prompts, replies, and logs are stored locally in `~/.local/share/ai-relay/`. The relay reads Claude's settings from `~/.claude/settings.json` and Codex's control socket from `$CODEX_HOME/app-server-control/app-server-control.sock`, defaulting to `~/.codex/`. Those local records are not part of this repository.

## Verification

~~~bash
python3 tests/verify_relay.py
python3 tests/verify_discovery.py
python3 tests/verify_roles.py
~~~

The checks use a mock Unix WebSocket server, synthetic session IDs, and a temporary home directory. They verify message framing, completed-turn correlation, long and fragmented replies, busy-to-idle waiting, round limits, approval handling, saved feedback delivery, and preservation of existing settings. Discovery and role checks cover closed processes, blank threads, checkout matching, startup choice and announcements, Codex-to-Claude-to-Codex handoffs, superseded tasks, draft preservation, and decision reports. They send no model requests and do not alter your live configuration.

Protocol references: [Codex App Server](https://developers.openai.com/codex/app-server/), [Codex slash commands](https://developers.openai.com/codex/cli/slash-commands/), and [Claude Code hooks](https://code.claude.com/docs/en/hooks).
