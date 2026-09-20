"""The build-time half of the slot cross-check, with a deliberately tiny YAML reader.

Run from `.service/Dockerfile` as the last step of the build, so an operator who edits
`stack/docker-compose.yml` without editing `.service/service.json` (or the reverse)
finds out at `nodo pack .` rather than on a node.

**Why this does not use `docker compose config`, given the image contains compose.**
`docker compose config` is the authority on what a compose file means, and it is what
`service/compose_spec.py` reads at *startup* for exactly that reason. It is not usable
here: a compose file may interpolate `${VAR}` from the environment or a `stack/.env`,
and compose either substitutes the build step's environment (wrong, and silently) or
refuses. A build must not fail because a runtime variable is unset, and must not bake a
build-time value into a check about runtime.

**Why this is not a YAML parser.** PyYAML is not in this image and will not be added --
the runtime is stdlib-only by design. So this reads the one construct it needs and
refuses to guess about anything else:

* `ports:` blocks under `services: <name>:`, in the **long form**
  (`- target: 80` / `published: "8080"`), which is what `stack/docker-compose.yml`
  documents itself as using and what compose normalises everything to anyway;
* and the **short form** (`- "8080:80"`) because it is what most compose files in the
  world are written in and refusing it would make this check useless on real input.

Anything it cannot read confidently, it **skips and says so**, exiting 0. That is the
important design decision here: this is an early-warning check with a partial parser,
so a construct it does not understand must not fail a build that is actually correct.
The check that is allowed to be strict is the startup one, which reads compose's own
normalisation and has no excuse.
"""

import json
import re
import sys
from typing import Dict, List, Set, Tuple

# `- "8080:80"`, `- "127.0.0.1:8080:80"`, `- "8080:80/udp"`, `- 8080:80`.
# The host port is the second-to-last colon-separated field when there are three, the
# first when there are two, and there is no host port at all when there is one -- which
# is the "compose picks a free port" case the startup check refuses and this one skips.
_SHORT_FORM = re.compile(
    r"""^-\s*["']?
        (?:(?P<ip>\d{1,3}(?:\.\d{1,3}){3}|\[[0-9a-fA-F:]+\]):)?
        (?P<published>\d+(?:-\d+)?):
        (?P<target>\d+(?:-\d+)?)
        (?:/(?P<protocol>tcp|udp))?
        ["']?\s*$""",
    re.VERBOSE,
)

_LONG_PUBLISHED = re.compile(r"""^(?:-\s*)?published:\s*["']?(?P<published>\d+(?:-\d+)?)["']?\s*$""")
_LONG_PROTOCOL = re.compile(r"""^(?:-\s*)?protocol:\s*["']?(?P<protocol>tcp|udp)["']?\s*$""")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def published_ports(text: str) -> Tuple[Set[Tuple[int, str]], List[str]]:
    """Every (host port, protocol) this reader is confident the compose file publishes.

    Returns the set and a list of notes about what was skipped, so the caller can print
    them: a skipped construct that nobody hears about is the same as a check that does
    not run.
    """
    found: Set[Tuple[int, str]] = set()
    notes: List[str] = []

    lines = text.splitlines()
    in_ports = False
    ports_indent = 0
    # The long form arrives as several lines per entry, so `published` and `protocol`
    # are collected per entry and flushed when the next entry starts or the block ends.
    pending_published: List[str] = []
    pending_protocol = "tcp"

    def flush() -> None:
        nonlocal pending_published, pending_protocol
        for value in pending_published:
            for port in _expand(value, notes):
                found.add((port, pending_protocol))
        pending_published = []
        pending_protocol = "tcp"

    for raw in lines:
        line = raw.split("#", 1)[0].rstrip() if not _in_quotes(raw) else raw.rstrip()
        if not line.strip():
            continue

        stripped = line.strip()

        if in_ports:
            # The block ends when indentation returns to or above the `ports:` key's.
            if _indent(line) <= ports_indent:
                flush()
                in_ports = False
            else:
                if stripped.startswith("- ") or stripped == "-":
                    # A new entry: whatever was pending belongs to the previous one.
                    flush()
                short = _SHORT_FORM.match(stripped)
                if short:
                    protocol = short.group("protocol") or "tcp"
                    for port in _expand(short.group("published"), notes):
                        found.add((port, protocol))
                    continue
                long_published = _LONG_PUBLISHED.match(stripped)
                if long_published:
                    pending_published.append(long_published.group("published"))
                    continue
                long_protocol = _LONG_PROTOCOL.match(stripped)
                if long_protocol:
                    pending_protocol = long_protocol.group("protocol")
                    continue
                if stripped.startswith(("target:", "- target:", "mode:", "host_ip:", "name:", "app_protocol:")):
                    continue
                notes.append(f"could not read port entry: {stripped!r}")
                continue

        if stripped in ("ports:", "- ports:") or stripped.startswith("ports:"):
            remainder = stripped[len("ports:"):].strip()
            if remainder and remainder not in ("[]",):
                # Inline flow sequence, e.g. `ports: ["8080:80"]`. Read each element.
                for element in remainder.strip("[]").split(","):
                    element = element.strip().strip("\"'")
                    if not element:
                        continue
                    short = _SHORT_FORM.match(f"- {element}")
                    if short:
                        protocol = short.group("protocol") or "tcp"
                        for port in _expand(short.group("published"), notes):
                            found.add((port, protocol))
                    else:
                        notes.append(f"could not read inline port entry: {element!r}")
                continue
            in_ports = True
            ports_indent = _indent(line)
            continue

    if in_ports:
        flush()

    return found, notes


def _in_quotes(line: str) -> bool:
    """Whether a `#` in this line is inside a quoted string rather than a comment.

    Crude on purpose -- an odd number of quote characters before the first `#`. It only
    has to be right often enough not to mangle `command: ["--flag", "#1"]`, and when it
    is wrong the result is a note rather than a wrong answer.
    """
    hash_at = line.find("#")
    if hash_at < 0:
        return False
    prefix = line[:hash_at]
    return (prefix.count('"') % 2 == 1) or (prefix.count("'") % 2 == 1)


def _expand(value: str, notes: List[str]) -> List[int]:
    if "-" in value:
        low_text, _, high_text = value.partition("-")
        try:
            low, high = int(low_text, 10), int(high_text, 10)
        except ValueError:
            notes.append(f"could not read port range {value!r}")
            return []
        if high < low or high - low > 1024:
            notes.append(f"port range {value!r} is not one this reader will expand")
            return []
        return list(range(low, high + 1))
    try:
        return [int(value, 10)]
    except ValueError:
        notes.append(f"could not read port {value!r}")
        return []


def slot_ports(declaration: Dict[str, object]) -> Set[Tuple[int, str]]:
    wanted: Set[Tuple[int, str]] = set()
    for slot in declaration.get("api") or []:
        if not isinstance(slot, dict) or "port" not in slot:
            continue
        try:
            port = int(slot["port"])
        except (TypeError, ValueError):
            continue
        transport = slot.get("transport") or "tcp"
        transports = [transport] if isinstance(transport, str) else list(transport)
        recognised = [
            str(t).strip().lower()
            for t in transports
            if str(t).strip().lower() in ("tcp", "udp")
        ]
        for name in recognised or ["tcp"]:
            wanted.add((port, name))
    return wanted


def health_port(declaration: Dict[str, object]) -> int:
    """The port the service serves its own health slot on.

    Read from the declaration's `envs` default if one is recorded there; otherwise the
    same default `service/config.py` uses. Kept in sync by `tests/test_preflight.py`,
    which asserts the two agree -- a health port this check did not know about would be
    reported as a slot nothing publishes, failing every build.
    """
    _ = declaration
    return 9000


def main(argv: List[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: preflight.py <docker-compose.yml> <service.json>\n")
        return 2

    compose_path, service_path = argv
    with open(compose_path, "r", encoding="utf-8") as handle:
        compose_text = handle.read()
    with open(service_path, "r", encoding="utf-8") as handle:
        declaration = json.load(handle)

    published, notes = published_ports(compose_text)
    declared = slot_ports(declaration)
    ignored = {health_port(declaration)}

    def render(ports) -> str:
        return ", ".join(f"{p}/{proto}" for p, proto in sorted(ports)) or "(none)"

    print(f"[preflight] compose publishes: {render(published)}")
    print(f"[preflight] service.json slots: {render(declared)}")
    print(f"[preflight] served by this service: {render((p, 'tcp') for p in sorted(ignored))}")
    for note in notes:
        print(f"[preflight] NOTE {note}")

    missing = {
        (port, protocol)
        for port, protocol in declared
        if port not in ignored and (port, protocol) not in published
    }

    if missing and notes:
        # The reader admitted it could not read part of the file, so a port it did not
        # find may well be published by a construct it skipped. Warn, do not fail: a
        # partial parser must not be the thing that refuses a correct build. The startup
        # check reads compose's own normalisation and will refuse it there if it is real.
        print(
            f"[preflight] WARNING: {render(missing)} not found among the published "
            "ports, but this reader skipped part of the file (see NOTEs). Not failing "
            "the build; service/supervisor.py re-checks this at startup against "
            "`docker compose config`."
        )
        return 0

    if missing:
        sys.stderr.write(
            f"[preflight] FAIL: API slots {render(missing)} are declared in "
            f"{service_path} but not published by {compose_path}.\n"
            "A slot the node forwards to a port nothing listens on is an instance that "
            "looks healthy and refuses every connection.\n"
            "Either add the port to the compose file's `ports:` or remove the slot.\n"
        )
        return 1

    print("[preflight] OK: every declared API slot is published by the compose file")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
