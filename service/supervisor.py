"""PID 1 for a compose stack: start dockerd, bring the stack up, supervise, tear down.

This is the program `service/entrypoint.sh` execs, and under nodo it is PID 1 in the
guest with no init system underneath it (`bash/build_ch_initramfs.sh` does
`exec switch_root /newroot "$ENTRYPOINT"`). That shapes every decision here:

* **nothing reaps for us.** dockerd, containerd, every `runc` and every
  `docker-proxy` become children of this process tree, and a PID 1 that does not reap
  leaves zombies until the pid table fills. So this process reaps, explicitly.
* **nothing forwards signals for us.** `docker stop` on the outside and the node's own
  teardown both arrive as SIGTERM to PID 1. If that is not turned into
  `docker compose down`, the containers are SIGKILLed with the VM and the stack's
  shutdown hooks never run.
* **if this exits, the kernel panics.** So an unexpected exception here must exit with
  a status and a logged reason, never propagate a traceback out of an unhandled thread.

Python rather than shell for this half, for one reason: it is supervision logic with
timeouts, a signal handler, a subprocess tree and a cross-check against JSON, and every
one of those in POSIX sh is a construct that is either wrong at the edges or
unreadable. The shell half is kept to what a shell is actually better at -- being the
`entry_path` the node execs, checking the image is what it thinks it is, and handing
over.

No shell is used *by* this module: every subprocess takes an argv list, so nothing an
operator writes in a compose file or an env var can become a command.
"""

import json
import os
import signal
import subprocess
import sys
import time
from typing import Dict, List, Optional, Sequence, Tuple

import compose_spec
import health
from config import (
    COMPOSE_PLUGIN,
    Config,
    ConfigError,
    DOCKER_BIN,
    DOCKERD_BIN,
    SERVICE_JSON,
    STACK_DIR,
    load,
)

LOG_PREFIX = "[compose-runner]"

# Poll interval while waiting for dockerd's socket. 0.25 s so a fast start is reported
# as fast (dockerd is usually up in a second or two) without spinning.
POLL_INTERVAL_S = 0.25

# How often the supervisor re-checks that the stack is still there, once up.
SUPERVISE_INTERVAL_S = 15


def log(message: str) -> None:
    """One line to stderr, unbuffered.

    stderr and flushed on every line because under nodo this goes to the guest's
    serial console, and a buffered log is an empty log exactly when the service dies
    -- which is the only time anyone reads it.
    """
    sys.stderr.write(f"{LOG_PREFIX} {message}\n")
    sys.stderr.flush()


class StackError(RuntimeError):
    """The stack could not be brought up, with the reason in the message."""


def _run(
    argv: List[str],
    timeout_s: int,
    env: Optional[Dict[str, str]] = None,
    cwd: Optional[str] = None,
) -> Tuple[int, str, str]:
    """Run argv to completion, bounded. Returns (code, stdout, stderr).

    Never `shell=True`, and argv is always a list: the compose file path and the
    project name reach this from the environment, and a string command would make
    either of them a place to write `; rm -rf /`.
    """
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout_s}s: {' '.join(argv)}"
    except OSError as e:
        return 127, "", f"cannot execute {argv[0]}: {e}"
    return completed.returncode, completed.stdout or "", completed.stderr or ""


class Supervisor:
    """The whole lifecycle, as an object so each step is separately testable.

    Every external effect goes through one of the small methods below rather than
    being inlined, which is what lets `tests/test_supervisor.py` drive a complete
    start/stop with no Docker present.
    """

    def __init__(self, config: Config):
        self.config = config
        self.dockerd: Optional[subprocess.Popen] = None
        self.health_server = None
        self.storage_driver_used: str = ""
        self.stopping = False
        self._document: Optional[Dict[str, object]] = None
        self._expected_services = 0

    # ------------------------------------------------------------------ environment
    def compose_env(self) -> Dict[str, str]:
        """The environment every `docker`/`docker compose` child is given.

        Inherited and then *overridden*, rather than replaced wholesale. Replacing it
        would be the stricter choice and is wrong here: a compose file's `${VAR}`
        interpolation is a documented compose feature, and an operator packing a stack
        that reads `${POSTGRES_PASSWORD}` from a `stack/.env` or from the node's
        `envs` is doing the ordinary thing. A replaced environment would silently
        interpolate those to empty strings.

        What is overridden is what must not be inherited: `DOCKER_HOST` points at the
        dockerd this process started and nothing else, so an inherited value cannot
        redirect the stack onto another daemon.
        """
        env = dict(os.environ)
        env["DOCKER_HOST"] = f"unix://{self.socket_path()}"
        env["COMPOSE_PROJECT_NAME"] = self.config.project_name
        # Compose reads this to find the plugin when invoked as `docker compose`; set
        # explicitly so it does not depend on a home directory that may not exist.
        env["DOCKER_CONFIG"] = "/var/lib/docker-cli"
        # Progress output that is readable in a line-oriented log rather than a TTY.
        # `plain` because the animated renderer writes cursor-movement escapes, and the
        # guest's console log is a file, not a terminal.
        env["COMPOSE_PROGRESS"] = "plain"
        env.setdefault("HOME", "/root")
        return env

    def socket_path(self) -> str:
        # /run is a tmpfs under nodo's initramfs, which is the right place for a socket
        # and the wrong place for image layers -- see config.DEFAULT_DATA_ROOT.
        return "/run/docker.sock"

    # ---------------------------------------------------------------------- dockerd
    def dockerd_argv(self, storage_driver: str) -> List[str]:
        """The dockerd command line, and why each flag is on it.

        `--iptables=false` is **not** here, and that is a decision worth stating: the
        stack's internal bridge network needs NAT to reach anything outside the VM, and
        turning dockerd's iptables management off would break egress for every
        container in the stack. What it requires of the guest kernel is listed in
        NODE-REQUIREMENTS.md, including the one finding that matters -- the guest kernel
        has `CONFIG_NF_TABLES=y` with no address family, so the `iptables` in this image
        is deliberately pointed at the **legacy** backend.
        """
        return [
            DOCKERD_BIN,
            "--host",
            f"unix://{self.socket_path()}",
            "--data-root",
            self.config.data_root,
            "--storage-driver",
            storage_driver,
            # No API on TCP, ever. The socket is local to this VM and the stack is
            # reached through its published ports; a dockerd listening on a port would
            # be root on this instance for anyone who reached it.
            "--iptables=true",
            # The bridge is dockerd's own default (`docker0`); named explicitly so the
            # log says what it is rather than leaving it implied.
            "--bridge",
            "docker0",
            # No live-restore: there is nothing to restore into. The VM is the
            # lifetime, and a daemon that kept containers running across its own
            # restart would be keeping them past the supervisor that owns them.
            "--live-restore=false",
            "--log-level",
            "info",
        ]

    def prepare_directories(self) -> None:
        """Create the data-root and the CLI config dir, or explain why not.

        Its own method so `start_dockerd` is testable without root: the directories are
        the one thing in the start path that needs privileges on a real filesystem, and
        a test of the storage-driver fallback should not have to be able to write
        /var/lib/docker.

        A failure here is fatal and named as such. dockerd would otherwise report it
        itself, but as a permissions error on a path an operator did not choose, twenty
        lines into its own startup log.
        """
        for directory in (self.config.data_root, "/var/lib/docker-cli"):
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError as e:
                raise StackError(
                    f"cannot create {directory}: {e}. dockerd needs a writable "
                    "data-root; see NODE-REQUIREMENTS.md for why this service cannot "
                    "declare read_only_filesystem."
                ) from None

    def start_dockerd(self) -> None:
        self.prepare_directories()

        drivers: Sequence[str]
        if self.config.storage_driver == "auto":
            # overlay2 first, vfs second. overlay2 shares layers and is what dockerd
            # wants; vfs copies every layer whole -- several times the disk and much
            # slower -- and is the only thing that works when the data-root's
            # filesystem cannot host an overlay upper dir. Which happens for real: an
            # overlayfs upper directory on top of another overlayfs is refused by the
            # kernel, and a nodo service that declared `read_only_filesystem: true`
            # gets exactly that (the rootfs is an overlay -- see
            # bash/build_ch_initramfs.sh), as does a container started on an overlay2
            # host with no volume at the data-root.
            drivers = ("overlay2", "vfs")
        else:
            drivers = (self.config.storage_driver,)

        last_error = ""
        for driver in drivers:
            argv = self.dockerd_argv(driver)
            log(f"starting dockerd (storage-driver={driver}): {' '.join(argv)}")
            # stdout/stderr inherited on purpose: dockerd's own log is the most
            # valuable thing in this container's output when a stack will not start,
            # and capturing it would mean holding it until something asked.
            try:
                self.dockerd = subprocess.Popen(argv, env=self.compose_env())
            except OSError as e:
                raise StackError(f"cannot execute dockerd at {DOCKERD_BIN}: {e}") from None

            ok, detail = self.wait_for_docker()
            if ok:
                self.storage_driver_used = driver
                log(f"dockerd is up on {self.socket_path()} with storage-driver={driver}")
                return

            last_error = detail
            log(f"dockerd did not become usable with storage-driver={driver}: {detail}")
            self.stop_dockerd()
            if driver != drivers[-1]:
                log("falling back to the next storage driver")

        raise StackError(
            f"dockerd never became usable (tried: {', '.join(drivers)}). Last error: "
            f"{last_error}"
        )

    def wait_for_docker(self) -> Tuple[bool, str]:
        """Block until `docker version` succeeds against the socket, or time out.

        The readiness test is a real API call, not the socket file appearing: dockerd
        creates the socket early and answers later, so waiting on the path reports
        ready while the next command still fails. `docker version` is the cheapest
        call that proves the daemon is actually serving.
        """
        deadline = time.monotonic() + self.config.dockerd_timeout_s
        last = "no attempt completed"
        while time.monotonic() < deadline:
            if self.dockerd is not None and self.dockerd.poll() is not None:
                # Exited already. Waiting out the rest of the timeout would turn a
                # 200 ms failure into a 60 s one for no new information.
                return False, f"dockerd exited with status {self.dockerd.returncode}"
            code, _, stderr = _run(
                [DOCKER_BIN, "version", "--format", "{{.Server.Version}}"],
                timeout_s=10,
                env=self.compose_env(),
            )
            if code == 0:
                return True, ""
            last = (stderr or "").strip().splitlines()[-1] if stderr.strip() else f"exit {code}"
            time.sleep(POLL_INTERVAL_S)
        return False, f"{last} (waited {self.config.dockerd_timeout_s}s)"

    def stop_dockerd(self) -> None:
        if self.dockerd is None:
            return
        if self.dockerd.poll() is None:
            self.dockerd.terminate()
            try:
                self.dockerd.wait(timeout=15)
            except subprocess.TimeoutExpired:
                log("dockerd did not exit on SIGTERM; killing it")
                self.dockerd.kill()
                try:
                    self.dockerd.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    log("dockerd is unresponsive to SIGKILL; continuing")
        self.dockerd = None

    # ------------------------------------------------------------------ offline load
    def load_offline_images(self) -> int:
        """`docker load` every `stack/images/*.tar`, if the operator packed any.

        The offline option, and the reason it exists: a `compose-runner` instance
        normally needs open egress purely to *pull images*, and an operator who would
        rather not grant that can `docker save` the stack's images into `stack/images/`
        before packing. Then the images are part of the content-addressed service --
        which is strictly stronger than a digest in a compose file, since the bytes
        themselves are hashed into the service id rather than merely referenced.

        Tars are loaded in sorted order so a stack whose images depend on load order
        behaves the same on every launch. A tar that fails to load is fatal: an image
        the operator packed and this service silently skipped would fail later as a
        pull attempt against a registry they may have declared no egress to.
        """
        directory = os.path.join(STACK_DIR, "images")
        if not os.path.isdir(directory):
            return 0
        tars = sorted(
            os.path.join(directory, name)
            for name in os.listdir(directory)
            if name.endswith(".tar")
        )
        if not tars:
            return 0

        log(f"loading {len(tars)} packed image archive(s) from {directory}")
        for path in tars:
            code, stdout, stderr = _run(
                [DOCKER_BIN, "load", "--input", path],
                timeout_s=self.config.up_timeout_s,
                env=self.compose_env(),
            )
            if code != 0:
                raise StackError(
                    f"`docker load` failed for {path}: {(stderr or stdout).strip()[:500]}"
                )
            for line in (stdout or "").splitlines():
                if line.strip():
                    log(f"  {line.strip()}")
        return len(tars)

    # --------------------------------------------------------------------- validate
    def compose_argv(self, *args: str) -> List[str]:
        """`docker compose -f <file> -p <project> <args...>`.

        `--project-directory` is set to the compose file's own directory so relative
        `build.context` paths in the packed stack resolve against the stack, not
        against this process's cwd.
        """
        return [
            DOCKER_BIN,
            "compose",
            "--file",
            self.config.compose_file,
            "--project-name",
            self.config.project_name,
            "--project-directory",
            os.path.dirname(self.config.compose_file) or STACK_DIR,
            *args,
        ]

    def read_compose_document(self) -> Dict[str, object]:
        """The normalised compose document, via compose's own `config --format json`.

        Done *before* `up`, because every reason this can fail is a reason not to
        start: a compose file with a syntax error, an unset `${VAR}` compose refuses to
        interpolate, a service with no image and no build. All of those are better as a
        message at second three than as a partially-started stack at second ninety.
        """
        code, stdout, stderr = _run(
            self.compose_argv("config", "--format", "json"),
            timeout_s=60,
            env=self.compose_env(),
        )
        if code != 0:
            raise StackError(
                f"`docker compose config` rejected {self.config.compose_file}: "
                f"{(stderr or stdout).strip()[:800]}"
            )
        return compose_spec.parse_config_json(stdout)

    def read_service_json(self) -> Dict[str, object]:
        """`.service/service.json` from inside the image, or `{}` if it is absent.

        Absent is tolerated and logged rather than fatal, for one specific case: this
        image run directly with `docker run` by someone testing it, where the packer's
        `.service/` was never copied in. Under `nodo pack .` it is there, because the
        Dockerfile copies it. What is *not* tolerated is a file that exists and is
        malformed -- that is a broken declaration, and reading past it would mean
        skipping the cross-check that is the whole reason this file is read.
        """
        try:
            with open(SERVICE_JSON, "r", encoding="utf-8") as handle:
                raw = handle.read()
        except FileNotFoundError:
            log(
                f"WARNING: {SERVICE_JSON} is not in this image, so the API-slot "
                "cross-check is skipped. Under `nodo pack .` it is present; this is "
                "the shape of a hand-run container."
            )
            return {}
        except OSError as e:
            raise StackError(f"cannot read {SERVICE_JSON}: {e}") from None
        return compose_spec.load_service_json(raw)

    def validate(self) -> Dict[str, object]:
        """Every check that can be made before anything is started.

        Returns the normalised compose document.
        """
        document = self.read_compose_document()
        names = compose_spec.service_names(document)
        self._expected_services = len(names)
        log(f"compose file declares {len(names)} service(s): {', '.join(names)}")

        declaration = self.read_service_json()
        api = declaration.get("api") or []
        if api:
            published = compose_spec.cross_check(
                document, api, ignore_ports=[self.config.health_port]
            )
            log(
                "API slots cross-checked against the compose file's published ports: "
                + (", ".join(f"{p}/{proto}" for p, proto in sorted(published)) or "(none)")
            )
        else:
            log(
                "no API slots declared in .service/service.json, so nothing to "
                "cross-check. The stack's published ports will be reachable only from "
                "inside this instance."
            )
        self._document = document
        return document

    # --------------------------------------------------------------------------- up
    def compose_up(self) -> None:
        argv = self.compose_argv("up", "--detach", "--remove-orphans", "--wait")
        log(f"bringing the stack up: {' '.join(argv)}")
        started = time.monotonic()
        # Inherited stdio: compose's pull and start progress is what an operator needs
        # while this is the slowest step in the launch.
        try:
            completed = subprocess.run(
                argv,
                timeout=self.config.up_timeout_s,
                env=self.compose_env(),
            )
        except subprocess.TimeoutExpired:
            raise StackError(
                f"`docker compose up` did not finish within COMPOSE_UP_TIMEOUT_S="
                f"{self.config.up_timeout_s}s. On a cold instance this is usually an "
                "image pull; raise the timeout or pack the images into stack/images/."
            ) from None
        except OSError as e:
            raise StackError(f"cannot execute docker compose: {e}") from None

        if completed.returncode != 0:
            # `--wait` makes a non-zero status mean "at least one container did not
            # reach a running/healthy state", which is exactly the condition this
            # service must not report as a successful launch. `ps` is dumped because
            # compose's own error does not say which container it was.
            _, stdout, _ = _run(
                self.compose_argv("ps", "--all", "--format", "json"),
                timeout_s=30,
                env=self.compose_env(),
            )
            for record in health.parse_ps(stdout):
                log(f"  container {record.get('Name')}: state={record.get('State')} health={record.get('Health')}")
            raise StackError(
                f"`docker compose up --wait` exited {completed.returncode}: the stack "
                "did not reach a running state. The container states above and the "
                "compose output show which service failed."
            )

        log(f"stack is up in {time.monotonic() - started:.1f}s")

    def compose_down(self) -> int:
        """Tear the stack down. Returns the exit status of `docker compose down`."""
        argv = self.compose_argv(
            "down",
            "--remove-orphans",
            "--timeout",
            str(self.config.down_timeout_s),
        )
        log(f"tearing the stack down: {' '.join(argv)}")
        code, stdout, stderr = _run(
            argv,
            # The subprocess budget is the container grace plus slack, so `down`'s own
            # SIGKILL escalation gets to happen inside this call rather than being cut
            # off by it.
            timeout_s=self.config.down_timeout_s + 60,
            env=self.compose_env(),
        )
        for stream in (stdout, stderr):
            for line in (stream or "").splitlines():
                if line.strip():
                    log(f"  {line.strip()}")
        if code != 0:
            log(f"WARNING: `docker compose down` exited {code}")
        return code

    # ---------------------------------------------------------------------- signals
    def install_signal_handlers(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._on_signal)
        # SIGCHLD is explicitly *not* handled with a handler that reaps, because the
        # subprocess module's own waiting would race with it. Reaping is done in the
        # supervise loop instead -- see `reap`.

    def _on_signal(self, signum: int, _frame: object) -> None:
        if self.stopping:
            log(f"signal {signum} while already shutting down; ignoring")
            return
        self.stopping = True
        log(f"signal {signum}: shutting the stack down")

    def reap(self) -> int:
        """Reap any exited children this process did not explicitly wait for.

        PID 1 with no init underneath it inherits every orphan in the VM, and dockerd's
        tree makes plenty: `docker-proxy` per published port, `runc` per container
        start, containerd-shim per container. Unreaped they accumulate as zombies for
        the life of the instance, and a long-lived stack that restarts containers will
        eventually exhaust the pid table.

        `WNOHANG` so this never blocks the supervise loop, and ECHILD is the ordinary
        "nothing to reap" answer rather than an error.
        """
        reaped = 0
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            except OSError:
                break
            if pid == 0:
                break
            # A child the subprocess module is tracking (dockerd) may be reaped here
            # first; Popen.poll() handles an already-reaped pid, so this is safe.
            reaped += 1
        return reaped

    # --------------------------------------------------------------------- lifecycle
    def start_health(self) -> None:
        provider = health.compose_ps_provider(
            self.compose_argv("ps", "--all", "--format", "json"),
            expected=self._expected_services,
        )
        # Started *before* dockerd and the stack in `run()`, so a caller polling it
        # during a slow launch gets an honest 503 rather than a refused connection.
        # That is the whole reason this slot exists.
        self.health_server = health.serve(self.config.health_port, provider)
        log(f"health slot listening on :{self.config.health_port}/health")

    def supervise(self) -> int:
        """Block until a signal or until the stack stops being up. Returns an exit code."""
        log("supervising; SIGTERM will bring the stack down")
        while not self.stopping:
            self.reap()
            if self.dockerd is not None and self.dockerd.poll() is not None:
                log(
                    f"FATAL: dockerd exited with status {self.dockerd.returncode} while "
                    "the stack was running"
                )
                return 1
            # Sliced sleep so a signal is acted on promptly rather than up to a whole
            # interval later. signal delivery interrupts sleep, but only the current
            # slice, and a 15 s pause before teardown would be a 15 s pause the node
            # sees as an instance ignoring SIGTERM.
            for _ in range(int(SUPERVISE_INTERVAL_S / POLL_INTERVAL_S)):
                if self.stopping:
                    break
                time.sleep(POLL_INTERVAL_S)
        return 0

    def run(self) -> int:
        self.install_signal_handlers()
        log(f"compose file:   {self.config.compose_file}")
        log(f"project name:   {self.config.project_name}")
        log(f"data root:      {self.config.data_root}")
        log(f"storage driver: {self.config.storage_driver}")

        try:
            self.start_dockerd()
            loaded = self.load_offline_images()
            if loaded:
                log(f"loaded {loaded} packed image archive(s)")
            self.validate()
            self.start_health()
            self.compose_up()
        except (StackError, compose_spec.ComposeSpecError) as e:
            log(f"FATAL: {e}")
            # Best-effort teardown of whatever did start, so a failed launch does not
            # leave containers running in a VM whose supervisor has given up.
            if self.dockerd is not None and self.dockerd.poll() is None:
                self.compose_down()
            self.stop_dockerd()
            return 1

        code = self.supervise()
        down = self.compose_down()
        self.stop_dockerd()
        # A clean `down` after a clean shutdown is a clean exit. A failed `down` is
        # reported in the status, because a stack that would not stop is a thing the
        # node should hear about rather than a warning in a log nobody reads.
        return code or (0 if down == 0 else 1)


def main(argv: Optional[List[str]] = None) -> int:
    _ = argv
    try:
        config = load()
    except ConfigError as e:
        log(f"FATAL: {e}")
        return 2
    return Supervisor(config).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
