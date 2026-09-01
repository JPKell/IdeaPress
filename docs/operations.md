# IdeaPress — operations

Running it for real: where things live, what to back up, and how to check it is healthy.

## Where things live

| What | Where | Override |
|---|---|---|
| Configuration | `$XDG_CONFIG_HOME/ideapress/config.toml` | `--config`, `IDEAPRESS_CONFIG` |
| Database | `$XDG_DATA_HOME/ideapress/ideapress.sqlite3` | `storage.database_url` |
| Project artifacts | `$XDG_DATA_HOME/ideapress/projects/` | `storage.project_dir` |

`ideapress config path` prints the first; `ideapress doctor` prints all three and whether they are
usable.

## Health

```bash
curl -s localhost:8767/api/v1/health          # database, backend, prompts
curl -s localhost:8767/api/v1/system/status   # plus what is running
ideapress doctor                              # everything, with remedies
```

Three components. **`backend` degraded is normal** when no model is running — projects still open,
units still read, exports still work.

## Backups

```bash
ideapress db backup --to ./backups/           # the database, consistently
ideapress project export <id> --to ./backups/ # one project, portable
```

The database backup is for this installation. The **project archive** is what survives it: it
imports into any IdeaPress of the same schema major, on any machine.

Restore:

```bash
ideapress db restore --from ./backups/ideapress-20260901.sqlite3
ideapress project import ./backups/local-inference.ideapress.zip
```

## PostgreSQL

```toml
[storage]
database_url = "postgresql+psycopg://user:pass@localhost/ideapress"
auto_migrate = false
```

`pip install 'ideapress[postgres]'`. `auto_migrate` defaults **off** here: a failed migration on
PostgreSQL cannot be rolled back automatically, so it is a decision rather than a side effect of
starting. Run `ideapress db upgrade` deliberately.

## Logs

Structured JSON, one object per line, carrying `request_id`, `project_id`, `unit_id`, `stage`,
`attempt`, `backend` and `model_canonical_id` — so one stage's whole history is a filter away.

```toml
[logging]
level = "INFO"
include_content = false   # true logs prompts and drafts. Your content. Debugging only
```

## One stage at a time

One stage runs per project, and one generation at a time process-wide. `execution.max_concurrent_stages`
above 1 is refused at startup rather than clamped: IdeaPress has no queue, and a second concurrent
generation means two models resident on a single-GPU machine, which degrades to CPU or OOMs with no
error the application could raise.

If you want a queue, that is what LoadCoach is for.

## Performance

The budgets IdeaPress holds itself to, and the command that proves them:

```bash
pytest -m performance
```

| Measure | Budget |
|---|---|
| Stage orchestration overhead per attempt (excluding inference) | ≤ 50 ms |
| Validation of a 5 000-word unit | ≤ 200 ms |
| Project load, 100 units | ≤ 300 ms |
| Export of 100 units to Markdown | ≤ 2 s |
| Export of 100 units to HTML | ≤ 5 s |
| Editor page render | ≤ 300 ms |
| Draft autosave round trip | ≤ 100 ms |

Model time is excluded from all of them: those are IdeaPress's own costs, and they are the ones it
can be held to.
