# Caller API

This Linux skill requires Python 3.10+, an authenticated Codex CLI supporting the app-server JSON-RPC protocol, and a compatible `hermes-herdr-pane-manager` installed in the active Hermes profile. It refuses operations without that skill. All pane creation and lock checks go through the pane manager's public CLI. A same-user private receiver runs inside a manager-provisioned pane and communicates with the local Codex process. It does not type prompts into a terminal or adopt arbitrary shells or agents.

Prefix each command with `python3 <skill-directory>/scripts/agent_manager.py`. Specify `--instance SESSION --workspace WORKSPACE_ID` on every operation and add `--pane PANE_ID` except when provisioning. The pane manager's `workspaces` operation can discover workspace IDs.

| Command | Extra required arguments | Result |
| --- | --- | --- |
| `provision` | `--cwd ABSOLUTE_PATH` | `pane_id`, `cwd`, `status: ready`. Optionally provide `--group` and `--suffix`. |
| `run` | `--agent codex --cwd ABSOLUTE_PATH --model MODEL` and `--prompt TEXT` or `--prompt-stdin` | `thread_id`, `turn_id`, `model`, `status: accepted`. |
| `steer` | `--prompt TEXT` or `--prompt-stdin`, plus `--turn-id TURN_ID` only for an active turn | Exact thread and turn IDs and final `answer`. An explicit turn ID must still be active; it will never silently start a follow-up. With no turn ID, the thread must be idle. |
| `change-model` | `--model MODEL` | `model`, `applies: next_turn`. Rejected while a turn is active. Provider acceptance of that model is verified by the next turn, not by this local update. |
| `get-remaining-tokens` | `--agent codex` | Account rate-limit windows `primary` and `secondary`, each with `remaining_percent`, `window_minutes`, and UTC `reset_at` if reported. This is account usage, not the context window or an exact count of remaining tokens. |
| `wait` | `--turn-id TURN_ID` | The completed latest confirmed turn's `answer` without submitting anything. |
| `recover` | None | Resume the exact saved thread after the receiver's Codex process has exited. Returns `thread_id`, `status: resumed` when idle. Sends no prompt. |

`steer` and `wait` accept `--seconds` (1–600; default 120). On `turn_pending`, retain the identified turn and wait again rather than submitting the prompt twice. `run` returns after native acceptance, not completion. Transport failures with uncertain state are not replayed. A receiver retries a transient Codex initialization failure once before sending any thread or prompt. After verifying the process has exited, `recover` resumes the saved thread without interrupting a live process or replaying a turn. Inspect the workspace and prior outcome before sending a new prompt. The skill does not transfer a thread between panes or automatically retry a failed or paused turn.

Workspace-write sandboxing is used with the named work directory; network is off unless `run --network` is explicitly authorized. The manager does not disable sandboxing, grant access requested by an agent, alter global Codex settings or trust, or handle approval prompts. It refuses to act on a zoom-locked or ambiguously identified pane. Pane verification and Codex submission are separate operations; they are not atomic with human zoom. The receiver is a local same-UID mechanism, not a security boundary between untrusted programs running as the same OS user. Scope this to trusted work and protect the account and skill paths.

The CLI emits UTF-8 JSON: `{"ok":true,"result":{...}}` on success (exit 0), or `{"ok":false,"error":{"code":"...","message":"..."}}` on operational failure (exit 1). Usage errors exit 2. Stdout of the Codex protocol and prompts are not copied into the pane; the pane hosts the receiver and the Codex process, not the interactive Codex TUI. Close a finished receiver pane through the pane manager after confirming it is idle and preserving any work. Old binding receipts are tiny private files and are not used to reconnect across receiver death.

Use `--prompt-stdin` to keep prompt text out of the process argument list. Prompts are limited to 32 KiB UTF-8 and local protocol frames to 1 MiB. If Codex reports no rate-limit snapshot, the quota call fails; an individual window or reset date is `null` when Codex does not report it.

Protocol reference: [Codex App Server](https://developers.openai.com/codex/app-server).
