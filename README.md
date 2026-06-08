# hermes-github-notifications-poller

A small GitHub notification watchdog for Hermes Agent.

It is designed to run from a script-only Hermes cron job:

1. Poll unread GitHub notifications with `gh api notifications`.
2. Filter to actionable notification reasons.
3. Atomically spool matching notifications to disk.
4. Immediately mark each spooled GitHub notification as done/read so it is not reprocessed.
5. Spawn a separate Hermes agent process to handle the spooled notification.
6. Print nothing when there is no work, so script-only cron stays silent.

The business logic lives in `bin/hermes-github-notifications-poller`.

## Requirements

- `gh` authenticated as the bot account.
- `hermes` available on PATH if spawning agents.
- Python 3.9+.

## Configuration

The poller is intentionally generic. Configure installation-specific behavior with environment variables or CLI flags:

| Environment variable | CLI flag | Purpose |
| --- | --- | --- |
| `GITHUB_BOT_LOGIN` | `--bot-login` | GitHub login for the bot account. If omitted, the spawned agent infers the bot from the authenticated `gh` account. |
| `GITHUB_DEFAULT_REVIEWER` | `--reviewer` | Optional default reviewer to request when the agent opens a ready PR. If omitted, no default reviewer is requested. |

Example:

```bash
GITHUB_BOT_LOGIN=my-github-bot \
GITHUB_DEFAULT_REVIEWER=octocat \
bin/hermes-github-notifications-poller
```

## Quick start

Dry-run without marking notifications read or spawning Hermes:

```bash
bin/hermes-github-notifications-poller --dry-run --verbose
```

Real run:

```bash
bin/hermes-github-notifications-poller
```

Default state directory:

```text
~/.hermes/github-notifications/
  pending/
  claimed/
  done/
  failed/
  logs/
  poller.log
```

## Recommended Hermes cron

Create a script-only cron job that runs every 3 minutes. Copy or symlink this repo script into `~/.hermes/scripts/` or pass the repo path if supported by your Hermes cron setup.

Example with the Hermes cron tool/CLI conceptually:

```bash
hermes cron create '3m'
```

Use:

- script: path to `bin/hermes-github-notifications-poller`
- no_agent: true

Script-only behavior matters: empty stdout means no message and no LLM call.

## Default filters

The poller processes notifications whose `reason` is one of:

- `assign`
- `mention`
- `review_requested`
- `team_mention`
- `comment`

For `comment`, the spawned Hermes agent is instructed to verify whether the bot is assigned, mentioned, requested for review, or otherwise expected to respond before acting.

## Safety model

The poller saves a spool file before marking the GitHub notification done/read. If spawning Hermes fails after the notification is marked done, the local spool file still exists and is moved to `failed/`.

The spawned Hermes prompt instructs the agent to:

- read the spool file;
- fetch current GitHub context with `gh`;
- ignore irrelevant notifications;
- acknowledge actionable requests;
- address review comments incrementally on bot-authored pull requests;
- avoid modifying code when only asked to review;
- create pull requests by default for code changes;
- request the configured default reviewer, if one is configured.

## Useful commands

List unread notifications:

```bash
gh api notifications --paginate
```

Mark a thread done/read:

```bash
gh api --method DELETE /notifications/threads/<thread_id>
```

Run tests:

```bash
python3 -m unittest discover -s tests -v
```
