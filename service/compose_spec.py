"""Reading a compose file's published ports, and checking them against the slots.

This is the module that exists because of one failure mode, and it is a quiet one.

A Celaut service's `api` slots in `.service/service.json` are what the node
*advertises* and what it writes port-forward rules for
(`vm_publish_port`, `src/virtualizers/microvm/network.py`). Inside the microVM, the
thing actually listening on those ports is a container published by
`docker compose up`. Nothing connects those two facts: the node does not read the
compose file, and compose does not read `service.json`. So a slot declared at 8080
against a stack that publishes 8081 produces an instance that boots, reports healthy,
advertises a slot, forwards traffic to a closed port, and fails only when a caller
tries to use it -- by which time the error is "connection refused" from somewhere
inside somebody else's VM.

That is worth failing at startup for, loudly, with both lists printed. Hence this
module, and hence it is a module rather than three lines of shell: it is the part of
`compose-runner` with actual logic in it, so it is the part that should be testable
without Docker, without a microVM, and without a network.

**It reads compose's own normalisation, not YAML.** `service/entrypoint.sh` runs
`docker compose config --format json` and feeds the result here. That is deliberate
and it is what keeps this file stdlib-only: the compose file format has short forms
(`"8080:80"`), long forms, ranges (`"8080-8082:80"`), `${VAR}` interpolation, `extends`,
multiple files and profiles, and re-implementing a subset of that in a hand-rolled
parser would be a second, worse compose whose disagreements with the real one are
exactly the bugs this module exists to prevent. `docker compose config` is the one
authority on what a compose file means, it ships in the image already, and it emits
every port in one normalised long form. So what is parsed here is a machine-generated
document with a known shape rather than a human-written one with many.
"""

import json
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


class ComposeSpecError(ValueError):
    """The compose document cannot be read, or does not match what was declared."""


# Compose's own default when a port entry names no protocol.
DEFAULT_PROTOCOL = "tcp"


def parse_config_json(document: object) -> Dict[str, object]:
    """The parsed `docker compose config --format json` output, or raise.

    Accepts the bytes/str compose wrote, or an already-decoded dict, because the
    entrypoint has the first and the tests mostly have the second.
    """
    if isinstance(document, (bytes, bytearray)):
        try:
            document = document.decode("utf-8")
        except UnicodeDecodeError as e:
            raise ComposeSpecError(f"compose config output is not UTF-8: {e}") from None

    if isinstance(document, str):
        text = document.strip()
        if not text:
            raise ComposeSpecError(
                "`docker compose config --format json` produced no output. "
                "The compose file is unreadable or empty."
            )
        try:
            document = json.loads(text)
        except ValueError as e:
            raise ComposeSpecError(
                f"`docker compose config --format json` did not produce JSON: {e}"
            ) from None

    if not isinstance(document, dict):
        raise ComposeSpecError(
            f"expected a JSON object from compose config, got {type(document).__name__}"
        )

    services = document.get("services")
    if not isinstance(services, dict) or not services:
        raise ComposeSpecError(
            "the compose file declares no services. A compose-runner stack needs at "
            "least one service to run."
        )

    return document


def service_names(document: Dict[str, object]) -> List[str]:
    """Every service the compose file declares, sorted, for logging."""
    services = document.get("services")
    if not isinstance(services, dict):
        return []
    return sorted(str(name) for name in services)


def _published_values(published: object) -> List[str]:
    """The `published` field as a list of strings, whatever shape it arrived in.

    Compose writes this as a string ("8080"), sometimes as an int (8080) depending on
    the version and how the entry was written, and as a range ("8080-8082"). An empty
    string means "pick a free host port", which is a thing compose supports and this
    service cannot use -- see `published_ports`.
    """
    if published is None:
        return []
    if isinstance(published, bool):
        # Guarded explicitly: bool is a subclass of int in Python, and a `published:
        # true` would otherwise silently become port 1.
        raise ComposeSpecError(f"`published: {published}` is not a port")
    if isinstance(published, int):
        return [str(published)]
    if isinstance(published, str):
        text = published.strip()
        return [text] if text else []
    raise ComposeSpecError(
        f"`published` must be a port or a port range, got {type(published).__name__}"
    )


def _expand_range(text: str) -> List[int]:
    """`"8080"` -> [8080]; `"8080-8082"` -> [8080, 8081, 8082].

    Ranges are real compose syntax and a stack that uses one publishes every port in
    it, so a slot check that did not expand them would reject a correct declaration.
    """
    if "-" not in text:
        return [_port_int(text)]

    low_text, _, high_text = text.partition("-")
    low = _port_int(low_text)
    high = _port_int(high_text)
    if high < low:
        raise ComposeSpecError(f"port range '{text}' ends below where it starts")
    # An upper bound on how many ports one entry may claim. Not arbitrary politeness:
    # this expands into a set, and `published: "1-65535"` would otherwise build a
    # 65k-element set at startup from a line in someone's compose file.
    if high - low > 1024:
        raise ComposeSpecError(
            f"port range '{text}' spans {high - low + 1} ports, which is more than "
            "this validator will expand (1024). Declare the ports the stack actually "
            "publishes."
        )
    return list(range(low, high + 1))


def _port_int(text: object) -> int:
    """A port number, or raise. Rejects everything a port is not."""
    if isinstance(text, bool):
        raise ComposeSpecError(f"'{text}' is not a port number")
    if isinstance(text, int):
        value = text
    else:
        stripped = str(text).strip()
        if not stripped or not stripped.isdigit():
            # `isdigit` rather than try/int, because int() accepts "+8080", "  8080  "
            # and unicode digits, none of which should read as a port here.
            raise ComposeSpecError(f"'{text}' is not a port number")
        value = int(stripped, 10)
    if value < 1 or value > 65535:
        raise ComposeSpecError(f"port {value} is outside 1-65535")
    return value


def published_ports(document: Dict[str, object]) -> Set[Tuple[int, str]]:
    """Every (host port, protocol) the stack publishes outside the compose network.

    This is the set that has to match the service's API slots, because these are the
    ports something will be listening on at the microVM's own address -- and the
    microVM's address *is* the service's address, which is the one fact that makes
    this whole approach work without a proxy. `ports:` in compose binds on the host,
    the host here is the service container, and the node forwards the declared slots
    to it.

    `expose:` is deliberately **not** read: it publishes nothing and is reachable only
    from inside the compose network, which is exactly the `redis` case in the example
    stack. A slot pointing at an `expose`d port would forward to nothing.

    A port published with no fixed host port (`published: ""`, or a bare
    `ports: ["80"]`, both meaning "choose one") is refused rather than ignored: the
    slot list in `service.json` is fixed at pack time and cannot describe a port
    chosen at runtime, so such an entry can never be reachable from outside and is
    much more likely a mistake than an intention.
    """
    services = document.get("services")
    if not isinstance(services, dict):
        raise ComposeSpecError("the compose document has no `services` mapping")

    found: Set[Tuple[int, str]] = set()

    for name in sorted(services):
        definition = services[name]
        if not isinstance(definition, dict):
            raise ComposeSpecError(f"service '{name}' is not a mapping")
        entries = definition.get("ports")
        if entries is None:
            continue
        if not isinstance(entries, list):
            raise ComposeSpecError(f"service '{name}' has a non-list `ports`")

        for entry in entries:
            if not isinstance(entry, dict):
                # `docker compose config --format json` always emits long form. A
                # string here means this document did not come from it, which is
                # worth saying rather than half-parsing.
                raise ComposeSpecError(
                    f"service '{name}' has a port entry that is not in long form "
                    f"({entry!r}). This document should be the output of "
                    "`docker compose config --format json`, which normalises every "
                    "entry; a short form here means it is not."
                )

            protocol = str(entry.get("protocol") or DEFAULT_PROTOCOL).strip().lower()
            if protocol not in ("tcp", "udp"):
                raise ComposeSpecError(
                    f"service '{name}' publishes protocol '{protocol}', which is "
                    "neither tcp nor udp"
                )

            values = _published_values(entry.get("published"))
            if not values:
                target = entry.get("target")
                raise ComposeSpecError(
                    f"service '{name}' publishes container port {target} with no "
                    "fixed host port. compose would pick a free one at runtime, and "
                    "an API slot in .service/service.json is fixed at pack time -- so "
                    "nothing outside the instance could ever reach it. Give it an "
                    f"explicit host port (e.g. `published: \"{target}\"`)."
                )

            for value in values:
                for port in _expand_range(value):
                    found.add((port, protocol))

    return found


def slot_ports(api: Iterable[object]) -> Set[Tuple[int, str]]:
    """Every (port, transport) the service declares as an API slot.

    Reads the `api` list from `.service/service.json` in the shape `docs/PACKING.md`
    documents: `port` required, `transport` a string or a list defaulting to `["tcp"]`.

    Only `tcp` and `udp` are read as transports. PACKING.md's own example shows
    `"transport": ["tcp", "http"]`, where `http` is an application protocol sitting in
    the transport field -- so a transport this does not recognise is *skipped* rather
    than refused, and the port is still checked under tcp. Refusing it would make this
    validator stricter than the packer it is validating against.
    """
    wanted: Set[Tuple[int, str]] = set()

    for index, slot in enumerate(api or []):
        if not isinstance(slot, dict):
            raise ComposeSpecError(f"api[{index}] is not an object")
        if "port" not in slot:
            raise ComposeSpecError(f"api[{index}] declares no port")
        port = _port_int(slot["port"])

        transport = slot.get("transport") or DEFAULT_PROTOCOL
        transports = [transport] if isinstance(transport, str) else list(transport)
        recognised = [
            str(t).strip().lower()
            for t in transports
            if str(t).strip().lower() in ("tcp", "udp")
        ]
        # A slot whose transport list names nothing recognisable is still a slot on a
        # port, and tcp is the packer's own default, so it is checked as tcp rather
        # than dropped -- dropping it would silently stop checking the slot.
        for name in recognised or [DEFAULT_PROTOCOL]:
            wanted.add((port, name))

    return wanted


def _render(ports: Iterable[Tuple[int, str]]) -> str:
    items = sorted(ports)
    return ", ".join(f"{port}/{protocol}" for port, protocol in items) or "(none)"


def cross_check(
    document: Dict[str, object],
    api: Sequence[object],
    ignore_ports: Optional[Iterable[int]] = None,
) -> Set[Tuple[int, str]]:
    """Refuse unless every declared API slot is a port the stack actually publishes.

    Returns the set of published (port, protocol) on success, for the caller to log.

    The check is deliberately **one-directional**: every slot must be published, but a
    published port need not be a slot. Those two are not symmetric, and the asymmetry
    is the useful part:

    * a **slot with no published port** is always broken. The node advertises it and
      forwards to a closed port inside the VM. There is no stack for which that is
      correct.
    * a **published port with no slot** is merely unreachable from outside, which is a
      perfectly ordinary thing for a stack to contain -- a metrics endpoint, an admin
      port an operator reaches over `nodo tunnel`, a port published for a sibling's
      benefit. Refusing it would force operators to either declare slots they do not
      want advertised or edit the upstream compose file they came here to avoid
      editing, which is the whole premise of this service.

    `ignore_ports` exists for exactly one member of the slot list: the health slot,
    which this service serves itself (`service/health.py`) and which no container in
    the compose file publishes. Passing it in rather than hardcoding it keeps this
    function about the general rule.
    """
    ignored = {int(p) for p in (ignore_ports or ())}
    published = published_ports(document)
    declared = slot_ports(api)

    missing = {
        (port, protocol)
        for port, protocol in declared
        if port not in ignored and (port, protocol) not in published
    }

    if missing:
        raise ComposeSpecError(
            "these API slots in .service/service.json are not published by the "
            f"compose file: {_render(missing)}. "
            f"The stack publishes: {_render(published)}. "
            f"Slots served by this service itself: {_render((p, 'tcp') for p in sorted(ignored))}. "
            "A slot the node forwards to a port nothing listens on is an instance that "
            "looks healthy and refuses every connection, so this is refused at startup "
            "rather than at the first request. Either add the port to the compose "
            "file's `ports:` or remove the slot."
        )

    return published


def load_service_json(raw: object) -> Dict[str, object]:
    """`.service/service.json` as a dict, or raise with the reason.

    Read at runtime from inside the image, which means the file the node packed and
    the file this check uses are the same bytes -- a copy of the slot list in an env
    var would be a second declaration to keep in sync.
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as e:
            raise ComposeSpecError(f".service/service.json is not valid JSON: {e}") from None
    if not isinstance(raw, dict):
        raise ComposeSpecError(".service/service.json must hold a JSON object")
    api = raw.get("api")
    if api is not None and not isinstance(api, list):
        raise ComposeSpecError(".service/service.json `api` must be a list")
    return raw
