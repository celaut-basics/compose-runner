# Fixtures

Real captures of what `docker compose ps --all --format json` actually writes, not
hand-written JSON. They exist because the key names (`Name`, `State`, `Health`,
`ExitCode`, capitalised) are easy to get wrong from memory, and a parser tested only
against its own author's assumptions proves nothing about the output it will meet.

Captured with Docker Compose **v5.0.1** against Docker Engine 29.5.2 (linux/arm64).

| file | what it is |
|---|---|
| `compose-ps-running.json` | The repo's own `stack/docker-compose.yml`, both containers up. The 200 case. |
| `compose-ps-exited.json` | Two services where one exits 1 immediately. `State: "exited"`, `ExitCode: 1` — the 503-degraded case. |
| `compose-ps-unhealthy.json` | One service with a `healthcheck:` that always fails. `State: "running"` **and** `Health: "unhealthy"` — which is the case that matters, because a summariser reading only `State` calls this healthy. |

One thing worth noting about all three: compose v5.0.1 writes **newline-delimited
objects**, not a JSON array, even though `--format json` suggests otherwise. Earlier and
later versions have done both, which is why `service/health.py:parse_ps` handles each —
and why these captures are committed rather than assumed.

How they were produced:

```sh
# running
docker compose -f stack/docker-compose.yml -p fixcap up -d --wait
docker compose -f stack/docker-compose.yml -p fixcap ps --all --format json

# exited: a second service whose entrypoint is `sh -c "exit 1"`
# unhealthy: a service whose healthcheck test is `["CMD", "false"]`, after ~12s
```
