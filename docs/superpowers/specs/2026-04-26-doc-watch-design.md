# Document Watch Automation Design

## Goal

Build a repository-local background watcher for `howindocs` that detects document changes, waits until at least 15 minutes have passed since the previous organization attempt, invokes Codex to organize changed documents, updates `index.md`, and pushes the resulting commit to GitHub.

## Scope

The automation is local to this Git repository. It provides explicit `start`, `stop`, `status`, `daemon`, and `run-once` commands through a Python script in `scripts/`. It does not install a system service, modify shell startup files, or depend on systemd.

The watcher monitors document-like files such as Markdown and plain-text notes. It ignores `.git/`, runtime state under `.codex-doc-watch/`, automation implementation files under `scripts/`, tests, and `docs/superpowers/` planning files so its own bookkeeping does not create an infinite organization loop.

## Architecture

`scripts/doc_watch.py` is a single-file standard-library Python utility.

It has three responsibilities:

- Process management: start a background daemon, stop it by PID, and report status.
- Change detection: inspect `git status --porcelain=v1 -z --untracked-files=all` and filter for document paths.
- Organization execution: when eligible, call `codex exec` with a constrained prompt that tells Codex to inspect the Git diff, organize documents into knowledge-base directories, update `index.md`, commit, and push.

Runtime files live under `.codex-doc-watch/`:

- `watcher.pid` stores the daemon PID.
- `state.json` stores the last organization attempt timestamp and pending paths.
- `logs/watcher.log` stores daemon output.
- `logs/codex-*.log` stores each Codex run.

## Trigger Rules

The daemon polls Git status at a configurable interval. Defaults:

- Poll interval: 30 seconds.
- Cooldown: 900 seconds.

If document changes are detected and no organization attempt has run in the last 900 seconds, the watcher starts a Codex run. If changes are detected during the cooldown window, it records them as pending and waits until the cooldown expires.

The cooldown is based on the last organization attempt, not only the last successful commit. This prevents repeated failed Codex runs from firing in a tight loop.

## Codex Prompt Contract

The generated prompt instructs Codex to:

- Inspect `git status`, `git diff --stat`, `git diff`, and untracked documents.
- Classify documents into `inbox/`, `notes/`, `projects/`, `references/`, or `maps/`.
- Preserve source content while improving filenames, headings, summaries, and links when appropriate.
- Create or update root `index.md` with categorized links and concise summaries.
- Avoid editing `.codex-doc-watch/`, `scripts/`, `tests/`, and `docs/superpowers/`.
- Run `git diff --check`.
- Commit with `docs: organize knowledge base updates` if there are document changes.
- Push to the configured remote and branch.

The watcher also performs a fallback commit and push if Codex exits successfully but leaves document changes uncommitted.

## Configuration

Environment variables:

- `CODEX_DOC_WATCH_INTERVAL_SECONDS`: poll interval.
- `CODEX_DOC_WATCH_COOLDOWN_SECONDS`: cooldown duration.
- `CODEX_DOC_WATCH_CODEX`: Codex executable path, default `codex`.
- `CODEX_DOC_WATCH_CODEX_ARGS`: Codex arguments, default `exec --full-auto --sandbox danger-full-access`.
- `CODEX_DOC_WATCH_REMOTE`: Git remote, default `origin`.
- `CODEX_DOC_WATCH_BRANCH`: push branch, default current branch.
- `CODEX_DOC_WATCH_DRY_RUN`: when set to `1`, log the prompt without invoking Codex.

## Error Handling

Codex output and errors are captured in timestamped log files. Failed attempts update the last-attempt timestamp, so the daemon waits for the next cooldown window before retrying. Push failures are logged and leave the local commit in place for manual repair.

## Testing

Use standard-library `unittest` to verify:

- Document path filtering accepts expected note files.
- Runtime, automation, and planning paths are ignored.
- Git porcelain `-z` status parsing handles modified, untracked, deleted, and renamed files.
- Cooldown calculation blocks and permits runs correctly.
- The Codex prompt contains the required changed paths, Git diff workflow, index update, commit, and push instructions.
