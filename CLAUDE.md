# sentinel-smdl

## Local gate commands (verified 2026-09-04, for /sentinel-review)

`sidecar-ai/` is its own subproject with its own import assumptions — running `pytest` from
the repo **root** fails collection (`ImportError: cannot import name 'summarize' from
'app'`) because its code assumes `sidecar-ai/` is the working directory, not that it's
callable from the parent. That is a cwd mismatch, not a real bug in the code.

Real working command: `pytest -q` run **from `sidecar-ai/`**, not from repo root. As of
2026-09-04: **19 passed, clean**. See `~/.claude/skills/sentinel-review/tiers.yaml`.

## Deploy and restart (2026-10-16)

The `smdl` container runs the code in this live checkout (bind mounts `app`, `static`, `data`). Keep the live checkout on `main` and fast-forward it; `git switch main` fails while another worktree holds `main`. Restart with `docker restart smdl`, then confirm `GET http://127.0.0.1:8096/health/bot` is `{"ready":true}`. Do not read the bot logs for content; count lines only.
