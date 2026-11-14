---
name: ai-relay
description: Connect the user's already-open Claude Code and Codex terminals and deliver work through ai-relay. Use for relay connection requests, including “여기서 해줘” when the conversation is about running a task in those terminals.
---

# Work in the user's relay terminals

Use the installed `ai-relay` executable. Detect the terminals the user opened, connect them, and send the actual authorized task into the selected initiator's terminal. The user should not need to type “hi,” provide session UUIDs, or explain the relay roles. Output belongs in those participating agent terminals; a log viewer alone does not satisfy the request.

- Run `ai-relay sessions --json` to discover live sessions. The calling Codex session is excluded automatically by `CODEX_THREAD_ID`; use `--exclude` if its ID is known but that environment variable is unavailable. Never relay to yourself.
- Use `ai-relay here` to choose a pair sharing a workspace, even when the calling conversation is in another directory. It prefers a new pair over the previous link. If multiple pairs remain, narrow with the user's intended `--repo` or ask which workspace they mean. Do not pick an arbitrary session.
- On a new connection, ask which agent initiates if the user has not specified or already authorized a role. Discover sessions while waiting. For an optional unanswered preference, allow time to respond, then state the current role or Claude default. Pass `--initiator claude` or `--initiator codex` to a noninteractive command. Keep an existing role for continuation tasks.
- Write the task to a temporary UTF-8 file, retaining the original objective, source paths, requested deliverables, and existing authorization. Then run `ai-relay here --initiator ROLE --task-file /absolute/path/to/task.txt`, with `--repo` only when needed to identify the intended pair. Setup announcements are not the task.
- Startup and task workers already use `nohup`. They wait for busy sessions and preserve drafts. Do not submit an unsent human draft or answer a permission dialog to force progress. A new connection or human prompt may cancel queued work; check the actual result.
- Verify delivery through `ai-relay status`, the matching `task_sent` event, and the selected session's submitted task or activity. A successful link or queued announcement alone is insufficient. If delivery fails, fix the connection and retry the still-authorized task; report what is actually pending.

Let the participating agents resolve routine choices and report a prominent **TL;DR — Decision**. Keep peer dialogue in their terminals. Relay messages do not authorize external writes, PR comments, Linear changes, emails, or other new actions. Apply the original user's permissions and task scope.
