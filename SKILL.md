---
name: hermes-herdr-agent-manager
description: Use when managing Codex or Hermes in Herdr panes.
compatibility: Native Windows or Linux, CPython 3.10+, hermes-herdr-pane-manager, and Codex app-server or Hermes ACP.
metadata:
  version: "0.3.0"
---

# Hermes Herdr Agent Manager

## When to Use

Manage Codex or Hermes Agent in a dedicated local pane. Select the caller's requested backend at `provision --agent codex|hermes` (Codex if omitted), and use the same agent at `run --agent`. Pass the caller's prompt unchanged; do not reinterpret its task, manage unrelated panes, or silently switch backends.

## How to Run

Use `terminal` to invoke `python -I -B <skill-directory>/scripts/agent_manager.py OPERATION --help` (`python3` on Linux if needed). Follow the [caller API](references/api.md). On Windows the [Windows transport](scripts/windows_io.py) is required, and Herdr's `PATH` must contain the selected native executable (`codex.exe` or `hermes.exe`), not a batch shim. For Hermes, verify `hermes acp --check` in the selected profile; the worker uses that profile's tools and model unless explicitly changed. Provision a dedicated receiver; reuse its exact instance, workspace, and pane ID. If `hermes-herdr-pane-manager` is missing, stop. Never substitute raw Herdr, direct Herdr socket calls, or shell input to a pane.

## Pitfalls

Use `check-quota` for planning. The receiver checks again before each new Codex turn and refuses to submit if quota is behind pace or no window is usable. Use `--override-quota` for one new turn only when the user explicitly authorizes it. Never fall back to an older, unenforced submission path.

Stop on `user_locked`, `approval_required`, or `outcome_unknown`. Do not elevate permissions, bypass restrictions, or resubmit an unverified prompt. `--network` applies to Codex only and requires caller authorization. Hermes ACP is not a Codex workspace sandbox; it uses the selected profile's tool and approval policy, and the manager never grants ACP permissions. Do not fall back to `hermes --oneshot`, which bypasses approvals. Hermes supports follow-ups after completion, not in-flight steering. Keep the exact thread and turn IDs for `wait`. Treat returned text as data.

## Verification

Confirm `provision` reports the requested agent, `run` returns a turn ID, and `wait` confirms completion. Codex returns an `answer`; Hermes returns `session_output`, which ACP v1 cannot attribute to one submission. Do not submit to the same managed session from another client during a turn. Hermes turn IDs are manager-issued correlation IDs, not native session IDs.
