"""A health slot that distinguishes "the VM is up" from "the stack is up".

Without this, a caller has no way to tell those apart, and they are very different
things here. A `compose-runner` instance boots a kernel, mounts a rootfs, starts
dockerd, pulls images and starts N containers -- a sequence that takes tens of
seconds on a cold instance and can fail at any step. Meanwhile the microVM's address
answers, and the node considers the instance launched. A caller connecting to a slot
during that window gets a TCP failure that looks exactly like the failure of a broken
stack.

So this serves one endpoint, on its own slot, whose whole job is to report what
`docker compose ps` says. Its own slot rather than a path on the stack's port,
because the stack's port belongs to the stack: the operator's compose file decides
what listens there, and a `compose-runner` that injected a `/health` route into
someone else's HTTP server would be modifying the application it promised not to
modify. Separate ports are also how the two remain distinguishable when the stack's
port is not HTTP at all -- a Postgres slot has no `/health` to add.

Python standard library only, no dependencies: this runs in an image whose reason for
existing is Docker's static binaries, and adding a web framework to report on them
would be the largest dependency in the service.
"""

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional, Tuple

# How long `docker compose ps` is given before the health endpoint gives up on it.
# Bounded because this runs on a request from outside: a `docker compose ps` wedged
# on an unresponsive dockerd must not wedge the health check too, or the one endpoint
# that exists to report trouble becomes the one that hangs during it.
PS_TIMEOUT_S = 10

# What `docker compose ps --format json` reports in `State` for a container that is
# up. Compose reports container state, not a health check, unless the compose file
# defines `healthcheck:` -- in which case `Health` carries "healthy"/"unhealthy" and
# `State` is still "running". Both are reported; neither is invented.
RUNNING_STATES = frozenset({"running"})


class _Handler(BaseHTTPRequestHandler):
    # Set by serve(); a class attribute because BaseHTTPRequestHandler is instantiated
    # per request by the server and there is no other seam to pass state through.
    status_provider: Callable[[], Tuple[int, Dict[str, object]]] = None  # type: ignore

    # Silences the default stderr line per request. The container's log is the service's
    # log, and a health probe every few seconds would bury the compose output that is
    # actually worth reading there.
    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler's spelling)
        # `path` may carry a query string; compare the path alone so `/health?x=1`
        # from a probe that adds cache-busting still works.
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path not in ("/health", "/"):
            self._respond(404, {"error": "not found", "paths": ["/health"]})
            return

        try:
            code, document = type(self).status_provider()
        except Exception as e:  # noqa: BLE001 -- a health endpoint must not 500 silently
            code, document = 503, {"status": "error", "detail": f"{type(e).__name__}: {e}"}
        self._respond(code, document)

    def _respond(self, code: int, document: Dict[str, object]) -> None:
        body = json.dumps(document, sort_keys=True).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def parse_ps(output: object) -> List[Dict[str, object]]:
    """`docker compose ps --format json` into a list of container records.

    Compose has emitted this two ways across versions, and both are handled because
    the version is pinned in the Dockerfile *today* and this should not break on the
    next bump:

    * a **JSON array** of objects (compose v2.21+ / v5 with `--format json`);
    * **newline-delimited** objects, one per line (earlier v2).

    Anything else returns an empty list rather than raising, because the caller is a
    health endpoint: "I could not read compose's output" is a degraded answer to
    report, not an exception to propagate into a 500.
    """
    if isinstance(output, (bytes, bytearray)):
        output = output.decode("utf-8", errors="replace")
    if isinstance(output, list):
        return [item for item in output if isinstance(item, dict)]
    if not isinstance(output, str):
        return []

    text = output.strip()
    if not text:
        return []

    try:
        document = json.loads(text)
    except ValueError:
        records: List[Dict[str, object]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                records.append(item)
        return records

    if isinstance(document, dict):
        return [document]
    if isinstance(document, list):
        return [item for item in document if isinstance(item, dict)]
    return []


def summarise(records: List[Dict[str, object]], expected: int = 0) -> Tuple[int, Dict[str, object]]:
    """An HTTP status and a body describing the stack, from compose's own records.

    The status codes are the contract, and each says something a caller can act on:

    * **200** -- every container compose knows about is running, and there is at least
      one. The stack is up.
    * **503** -- compose knows about no containers at all (nothing started yet, or
      everything has gone), or fewer than the compose file declares, or at least one
      is not running. Retry; it may be starting.

    503 rather than 500 for a down stack, deliberately: the service is working
    correctly and reporting honestly that what it supervises is not ready. 500 would
    mean this server broke.

    `expected` is the number of services the compose file declares, so that a stack
    which started two of three containers reads as not-ready rather than as ready --
    compose's `ps` only lists containers that exist, so "all running" over a short
    list is a trap without it.
    """
    containers = []
    unhealthy = []
    for record in records:
        # Compose's JSON keys are capitalised (`Name`, `State`, `Health`, `Service`).
        # `.get` on each with a fallback rather than indexing: an unreadable record
        # should degrade this report, not raise inside a health check.
        name = str(record.get("Name") or record.get("name") or "?")
        service = str(record.get("Service") or record.get("service") or "?")
        state = str(record.get("State") or record.get("state") or "").strip().lower()
        health = str(record.get("Health") or record.get("health") or "").strip().lower()
        exit_code = record.get("ExitCode", record.get("exitCode"))

        entry: Dict[str, object] = {"name": name, "service": service, "state": state}
        if health:
            entry["health"] = health
        if isinstance(exit_code, int) and exit_code != 0:
            entry["exit_code"] = exit_code
        containers.append(entry)

        # A container with a `healthcheck:` in the compose file that reports
        # "unhealthy" is not ready even though its state is "running" -- that is the
        # entire point of the operator having written the health check.
        if state not in RUNNING_STATES or health == "unhealthy":
            unhealthy.append(entry)

    containers.sort(key=lambda item: str(item.get("name")))
    document: Dict[str, object] = {
        "containers": containers,
        "running": len(containers) - len(unhealthy),
        "total": len(containers),
    }
    if expected:
        document["expected"] = expected

    if not containers:
        document["status"] = "starting"
        document["detail"] = "compose reports no containers yet"
        return 503, document

    if unhealthy:
        document["status"] = "degraded"
        document["detail"] = "not every container is running"
        return 503, document

    if expected and len(containers) < expected:
        document["status"] = "starting"
        document["detail"] = (
            f"{len(containers)} of {expected} declared services have containers"
        )
        return 503, document

    document["status"] = "ok"
    return 200, document


def compose_ps_provider(
    argv: List[str],
    expected: int = 0,
    runner: Optional[Callable[[List[str]], Tuple[int, str, str]]] = None,
) -> Callable[[], Tuple[int, Dict[str, object]]]:
    """A status provider that shells out to `docker compose ps` and summarises it.

    `argv` is the full command as a list -- never a string, and never through a shell.
    `runner` is injected so the tests can exercise every branch of the summary without
    Docker; in the image the default runner is `subprocess.run`.
    """

    def default_runner(command: List[str]) -> Tuple[int, str, str]:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=PS_TIMEOUT_S,
                # A replaced-but-minimal env is not used here: `docker compose` needs
                # DOCKER_HOST and PATH from the entrypoint's environment to find the
                # daemon it was told about. Inheriting is the correct behaviour for a
                # child of the supervisor that configured it.
            )
        except subprocess.TimeoutExpired:
            return 124, "", f"`docker compose ps` did not answer within {PS_TIMEOUT_S}s"
        except OSError as e:
            return 127, "", f"cannot run docker compose: {e}"
        return completed.returncode, completed.stdout or "", completed.stderr or ""

    run_it = runner or default_runner

    def provider() -> Tuple[int, Dict[str, object]]:
        code, stdout, stderr = run_it(list(argv))
        if code != 0:
            # dockerd down, socket gone, compose project missing. All of them mean the
            # same thing to a caller -- the stack cannot be confirmed up -- and the
            # stderr is what tells an operator which.
            return 503, {
                "status": "error",
                "detail": (stderr or stdout or "docker compose ps failed").strip()[:500],
                "exit_code": code,
            }
        return summarise(parse_ps(stdout), expected=expected)

    return provider


def serve(
    port: int,
    status_provider: Callable[[], Tuple[int, Dict[str, object]]],
) -> ThreadingHTTPServer:
    """Start the health server on a daemon thread and return it.

    A thread rather than a process, and a daemon thread rather than a joined one: the
    supervisor in `entrypoint.sh`'s Python half owns the process lifetime, and a
    health server outliving the supervisor would keep an instance reporting on a stack
    nobody is watching.

    Bound to 0.0.0.0 because the point of this port is to be reached from outside the
    microVM, which is where every caller is.
    """
    handler = type("_BoundHandler", (_Handler,), {"status_provider": staticmethod(status_provider)})
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    thread = threading.Thread(target=server.serve_forever, name="health", daemon=True)
    thread.start()
    return server
