# Hermes Herdr Agent Manager

Run Codex or Hermes Agent in a dedicated local Herdr pane. Submit a turn, wait for the answer, send a follow-up, and choose the model for future turns. Codex additionally supports active-turn steering, account quota windows, and thread recovery. All pane operations go through the pane-manager skill.

## Install and lock the skill

Install CPython 3.10+, Herdr, and [Hermes Herdr Pane Manager](https://github.com/milyin/hermes-herdr-pane-manager) in the same Hermes profile. Install only the backends you need:

- **Codex:** authenticate the Codex CLI with `app-server` support and complete any required native sandbox setup yourself. The manager does not change sandbox settings or use an elevated fallback.
- **Hermes:** configure the selected profile's model, provider, credentials, and tool policy. Run `hermes -p PROFILE_NAME acp --check`. This backend launches `hermes acp`, not an interactive terminal or one-shot process.

The selected executable must be on the Herdr server's `PATH`; on Windows use `codex.exe` or `hermes.exe`, not WSL or a batch shim.

Set the profile name in the example, then clone the complete skill and apply the lock before first use. The commands use `hermes config path` to locate the chosen profile without guessing its home.

Windows (PowerShell):

```powershell
$profileName = 'default'  # Replace with the profile you use.
$configPath = hermes -p $profileName config path
if ($LASTEXITCODE -ne 0 -or -not $configPath) { throw 'Could not locate the Hermes profile' }
$skillDir = Join-Path (Split-Path -Parent $configPath) 'skills/hermes-herdr-agent-manager'
git clone https://github.com/milyin/hermes-herdr-agent-manager.git $skillDir
if ($LASTEXITCODE -ne 0) { throw 'Skill clone failed' }
hermes -p $profileName curator lock hermes-herdr-agent-manager
if ($LASTEXITCODE -ne 0) { throw 'Skill lock failed' }
hermes -p $profileName curator status
python -I -B (Join-Path $skillDir 'scripts/agent_manager.py') --help
```

Linux:

```sh
profile_name=default  # Replace with the profile you use.
config_path="$(hermes -p "$profile_name" config path)" &&
skill_dir="$(dirname "$config_path")/skills/hermes-herdr-agent-manager" &&
git clone https://github.com/milyin/hermes-herdr-agent-manager.git "$skill_dir" &&
hermes -p "$profile_name" curator lock hermes-herdr-agent-manager &&
hermes -p "$profile_name" curator status &&
python3 -I -B "$skill_dir/scripts/agent_manager.py" --help
```

Ensure `curator status` lists `hermes-herdr-agent-manager` as locked before starting a new session in that profile. Cloning does not apply the lock. It keeps the skill usable while blocking edits by Hermes during normal use and background review. It is a [Hermes policy lock](https://hermes-agent.nousresearch.com/docs/user-guide/features/curator#locking-a-skill), not a filesystem permission; you can still update the checkout manually with Git. Lock the pane-manager dependency in the same profile using its own install instructions.

See the [caller API](references/api.md) for exact arguments and results. If calling the script outside Hermes for a non-default profile, set `HERMES_HOME` to that profile's home before invoking the script. Use `provision` to start a dedicated receiver; the skill will not adopt a pre-existing pane or retry a prompt whose outcome is unknown.

## Use Hermes as the worker

From the skill directory, replace `SESSION`, `WORKSPACE_ID`, and `ABSOLUTE_PATH` with the actual values. Use the returned pane and turn IDs in the next commands.

```sh
python -I -B scripts/agent_manager.py provision --agent hermes --instance SESSION --workspace WORKSPACE_ID --cwd "ABSOLUTE_PATH"
python -I -B scripts/agent_manager.py run --agent hermes --instance SESSION --workspace WORKSPACE_ID --pane PANE_ID --cwd "ABSOLUTE_PATH" --prompt "Summarize this project."
python -I -B scripts/agent_manager.py wait --instance SESSION --workspace WORKSPACE_ID --pane PANE_ID --turn-id TURN_ID
```

Use `steer` without `--turn-id` for a follow-up after completion. Omitting `--model` on `run --agent hermes` uses the profile default. Hermes returns observed `session_output`, not a turn-attributed `answer`. Do not submit to the same session from another client during a managed turn. Hermes uses its ACP profile policy, not a workspace-write sandbox supplied by this skill; the manager never grants ACP approval requests. Existing Codex workflows may still omit `--agent` on `provision`.

## Verify

Run `python -B -m unittest discover -s tests -v` from the checkout (`python3` on Linux if needed). The default suite needs neither account credentials nor Herdr. It tests the ACP wire contract with a local fixture and exercises native Windows pipes when run on Windows.

For the opt-in end-to-end smoke test, supply a disposable Herdr instance whose name starts with `agent-manager-smoke-`, a supported model ID, and an authenticated Codex account. It creates a temporary workspace, runs two short model turns, reads quota, and verifies recovery behavior. On Linux it also tests resuming the exact thread after its own Codex subprocess exits. Stop the disposable instance after testing.

To smoke-test Hermes instead, set `AGENT_MANAGER_HERMES_LIVE=1` in place of `AGENT_MANAGER_LIVE=1`. Keep the disposable Herdr instance, but omit `AGENT_MANAGER_TEST_MODEL`. This test uses the configured Hermes profile, creates a test session in it, and checks the second turn can recall the first.

Windows (PowerShell):

```powershell
$env:AGENT_MANAGER_LIVE = '1'
$env:AGENT_MANAGER_TEST_INSTANCE = 'agent-manager-smoke-local'
$env:AGENT_MANAGER_TEST_MODEL = 'YOUR_SUPPORTED_MODEL'
python -B -m unittest discover -s tests -v
```

Linux:

```sh
AGENT_MANAGER_LIVE=1 AGENT_MANAGER_TEST_INSTANCE=agent-manager-smoke-local \
  AGENT_MANAGER_TEST_MODEL=YOUR_SUPPORTED_MODEL python3 -B -m unittest discover -s tests -v
```
