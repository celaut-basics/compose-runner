"""What the environment is allowed to say, and what it means when it says nothing.

Its own module so the whole contract of `envs` in `.service/service.json` is one file
to read, and so it is testable without Docker.

Every value is read once, at start, and **refused loudly rather than clamped**. A
`COMPOSE_UP_TIMEOUT_S=abc` silently treated as 300 is a service whose declared limit
is not the limit it enforces, and the limits here are what stand between "the stack
did not start" and an instance that hangs forever holding a node's resources.
"""

import os
from dataclasses import dataclass
from typing import Dict, Optional

# Where the Dockerfile puts things. Not configurable: they are part of the
# content-addressed filesystem, and a path that could be repointed would be a way to
# run a compose file this service id does not contain.
DOCKER_BIN = "/opt/docker/bin/docker"
DOCKERD_BIN = "/opt/docker/bin/dockerd"
# Where the Docker CLI finds the compose plugin. `/usr/local/lib/docker/cli-plugins` is
# one of the directories it searches; /opt/docker/cli-plugins is not, and putting it
# there produced a `docker compose` that did not exist as a subcommand at all -- see the
# comment on the COPY in .service/Dockerfile.
COMPOSE_PLUGIN = "/usr/local/lib/docker/cli-plugins/docker-compose"
SERVICE_JSON = "/.service/service.json"
STACK_DIR = "/app/stack"
DEFAULT_COMPOSE_FILE = STACK_DIR + "/docker-compose.yml"

# Where dockerd keeps images, layers and container filesystems.
#
# This is the single most consequential path in the service, and it is on the **rootfs**
# rather than under /tmp for a reason that is a fact about the node, not a preference:
#
# nodo's initramfs mounts /tmp as a **tmpfs** (bash/build_ch_initramfs.sh: `mount -t
# tmpfs -o mode=1777,nosuid,nodev tmpfs /newroot/tmp`). A tmpfs is RAM. Putting
# /var/lib/docker there would mean every image layer the stack pulls is charged against
# the instance's *memory* limit, not its disk -- so a 1.5 GB image on a 1 GB instance
# does not fill a disk, it OOMs the guest. It would also make `disk_space` in
# service.json describe nothing the service uses.
#
# The rootfs is writable ext4 on the ordinary path, sized to at least
# `at_init.disk_space` (src/virtualizers/microvm/limits.py: `initial_rootfs_size_bytes`
# floors the image at the declared figure), which is exactly the property dockerd needs
# and exactly what the declared 4 GB is for. See NODE-REQUIREMENTS.md for why this
# service therefore cannot declare `read_only_filesystem: true`.
DEFAULT_DATA_ROOT = "/var/lib/docker"

# The health slot. Its own port, and 9000 rather than 8080 so the example stack's own
# published port is the more obvious one.
DEFAULT_HEALTH_PORT = 9000

# Storage drivers this service will ask dockerd for. `overlay2` is what dockerd wants
# and what the guest kernel supports (CONFIG_OVERLAY_FS=y); `vfs` is the fallback that
# works on any filesystem at the cost of copying every layer instead of sharing it.
# `auto` lets the entrypoint try overlay2 and fall back, which is the default.
STORAGE_DRIVERS = ("auto", "overlay2", "vfs")


class ConfigError(ValueError):
    """The environment this service was launched with cannot be honoured."""


def _int_env(
    env: Dict[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    text = raw.strip()
    # `isdigit` before int(), because int() accepts "+300", "1_0" and unicode digits,
    # none of which should read as a number of seconds here.
    if not text.isdigit():
        raise ConfigError(
            f"{name}={text!r} is not a whole number. "
            f"Leave it unset for the default ({default})."
        )
    value = int(text, 10)
    if value < minimum or value > maximum:
        raise ConfigError(
            f"{name}={value} is outside the accepted range [{minimum}, {maximum}]."
        )
    return value


def _project_name(raw: Optional[str]) -> str:
    """A compose project name, validated against compose's own rule.

    Compose requires `[a-z0-9][a-z0-9_-]*`, and it does not merely complain about a
    name outside that: it refuses the whole invocation. Since this value ends up as
    the prefix of every container, network and volume the stack creates, a rejection
    here at start -- naming the rule -- is better than the same rejection arriving as
    compose's error in the middle of a launch.
    """
    name = (raw or "stack").strip()
    if not name:
        name = "stack"
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789_-")
    if name[0] not in set("abcdefghijklmnopqrstuvwxyz0123456789") or not set(name) <= allowed:
        raise ConfigError(
            f"COMPOSE_PROJECT_NAME={name!r} is not a compose project name. It must "
            "start with a lowercase letter or digit and contain only lowercase "
            "letters, digits, hyphens and underscores -- compose's own rule, which it "
            "refuses rather than normalises."
        )
    return name


@dataclass(frozen=True)
class Config:
    project_name: str
    compose_file: str
    up_timeout_s: int
    dockerd_timeout_s: int
    storage_driver: str
    data_root: str
    health_port: int
    down_timeout_s: int


def load(env: Optional[Dict[str, str]] = None) -> Config:
    """Read the environment into a Config, or raise ConfigError explaining why not."""
    env = dict(os.environ if env is None else env)

    compose_file = (env.get("COMPOSE_FILE") or DEFAULT_COMPOSE_FILE).strip()
    if not compose_file:
        compose_file = DEFAULT_COMPOSE_FILE
    if not compose_file.startswith("/"):
        # Relative would resolve against whatever the supervisor's cwd happens to be,
        # which is not something a service's contract should depend on.
        raise ConfigError(
            f"COMPOSE_FILE={compose_file!r} must be an absolute path inside the image."
        )
    # Compose's own COMPOSE_FILE supports several paths separated by the path separator.
    # That is deliberately not supported here: the cross-check in compose_spec.py reads
    # one normalised document, and multi-file overlays are expressible by packing a
    # single merged file. Refused rather than silently using the first.
    if os.pathsep in compose_file or "," in compose_file:
        raise ConfigError(
            f"COMPOSE_FILE={compose_file!r} names more than one file. This service "
            "takes a single compose file; merge your overlays into one and pack that."
        )

    # 600 s default. An image pull on a cold instance over a slow link is the dominant
    # term, and a stack of three or four images routinely exceeds a minute. The ceiling
    # is what turns "the stack cannot start" into an exit rather than an instance that
    # holds a node's memory forever waiting on a registry.
    up_timeout_s = _int_env(env, "COMPOSE_UP_TIMEOUT_S", default=600, minimum=5, maximum=7200)

    # dockerd itself has nothing to download and is a local daemon starting on a local
    # socket, so 60 s is generous. It is separate from the compose budget because the
    # two failures are different and an operator reading a log should be able to tell
    # "the runtime never came up" from "the stack never came up".
    dockerd_timeout_s = _int_env(env, "DOCKERD_TIMEOUT_S", default=60, minimum=5, maximum=600)

    # How long `docker compose down` is given on SIGTERM. Below the node's own kill
    # grace by default, so a clean shutdown is normally what happens rather than a
    # SIGKILL landing mid-teardown and leaving the containers' state half-written.
    down_timeout_s = _int_env(env, "COMPOSE_DOWN_TIMEOUT_S", default=30, minimum=1, maximum=600)

    storage_driver = (env.get("DOCKERD_STORAGE_DRIVER") or "auto").strip().lower()
    if storage_driver not in STORAGE_DRIVERS:
        raise ConfigError(
            f"DOCKERD_STORAGE_DRIVER={storage_driver!r} is not one of "
            f"{', '.join(STORAGE_DRIVERS)}."
        )

    data_root = (env.get("DOCKERD_DATA_ROOT") or DEFAULT_DATA_ROOT).strip()
    if not data_root.startswith("/"):
        raise ConfigError(f"DOCKERD_DATA_ROOT={data_root!r} must be an absolute path.")

    health_port = _int_env(
        env, "HEALTH_PORT", default=DEFAULT_HEALTH_PORT, minimum=1, maximum=65535
    )

    return Config(
        project_name=_project_name(env.get("COMPOSE_PROJECT_NAME")),
        compose_file=compose_file,
        up_timeout_s=up_timeout_s,
        dockerd_timeout_s=dockerd_timeout_s,
        storage_driver=storage_driver,
        data_root=data_root,
        health_port=health_port,
        down_timeout_s=down_timeout_s,
    )
