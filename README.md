# Hermes Herdr Agent Manager

Control a Codex agent in a dedicated local Herdr pane: launch a turn, steer it or send a follow-up, choose the next-turn model, and read account quota windows. This is technical orchestration, not a task planner. All Herdr operations go through the pane manager skill.

## Install and lock the skill

Install CPython 3.10+, Herdr, Codex with `app-server` support, and [Hermes Herdr Pane Manager](https://github.com/milyin/hermes-herdr-pane-manager) in the same Hermes profile. On Windows, install the native `codex.exe` and put it on `PATH` for the Herdr server; WSL and a `codex.cmd` shim aren't used. Authenticate Codex separately and complete any required native sandbox setup yourself. The skill does not change the sandbox or use an elevated fallback.

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

## Verify

Run `python -B -m unittest discover -s tests -v` from the checkout (`python3` on Linux if needed). The default suite needs neither account credentials nor Herdr. Windows tests exercise the native same-user pipe and child stdio transport.

For the opt-in end-to-end smoke test, supply a disposable Herdr instance whose name starts with `agent-manager-smoke-`, a supported model ID, and an authenticated Codex account. It creates a temporary workspace, runs two short model turns, reads quota, and verifies recovery behavior. On Linux it also tests resuming the exact thread after its own Codex subprocess exits. Stop the disposable instance after testing.

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
