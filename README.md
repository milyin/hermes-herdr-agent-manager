# Hermes Herdr Agent Manager

Control a Codex agent in a dedicated local Herdr pane: launch a turn, steer it or send a follow-up, choose the next-turn model, and read account quota windows. This is technical orchestration, not a task planner. No raw Herdr commands are used by the skill.

## Install

Install Python 3.10+, Codex, and [Hermes Herdr Pane Manager](https://github.com/milyin/hermes-herdr-pane-manager) in the active profile. Authenticate Codex separately. This release runs on Linux. Clone the complete skill (not only `SKILL.md`):

```sh
skills="${HERMES_HOME:-$HOME/.hermes}/skills"
mkdir -p "$skills"
git clone https://github.com/milyin/hermes-herdr-agent-manager.git "$skills/hermes-herdr-agent-manager"
python3 "$skills/hermes-herdr-agent-manager/scripts/agent_manager.py" --help
```

Start a new Hermes session after installation. See the [caller API](references/api.md) for the exact arguments and results. Provisioning through this skill is required; it cannot start a worker in a pre-existing arbitrary pane. It never auto-approves new permissions or reruns work whose outcome is uncertain.

## Verify

From the checkout run `python3 -B -m unittest discover -s tests -v`. The default suite does not require account credentials. For a real lifecycle test, supply a separately provisioned disposable Herdr instance whose name starts with `agent-manager-smoke-`, plus an authenticated Codex account. The test creates a workspace, makes two short model calls, reads account quota, and tests thread recovery. Stop the disposable instance after testing.

```sh
AGENT_MANAGER_LIVE=1 AGENT_MANAGER_TEST_INSTANCE=agent-manager-smoke-local \
  AGENT_MANAGER_TEST_MODEL=YOUR_SUPPORTED_MODEL python3 -B -m unittest discover -s tests -v
```
