# compose-runner

A [Celaut](https://github.com/celaut-project/nodo) service that runs an existing
`docker-compose.yml` application as a single service instance.

## Why this exists

A Celaut service is one container, built from one `.service/Dockerfile`. Most
real applications are not that -- they are a `docker-compose.yml` with two or
three services wired together (an app plus a database, a worker plus a queue),
and today the only way onto a nodo is rewriting that stack into a single
Dockerfile by hand, which is real work and has to be redone every time upstream
changes theirs.

`compose-runner` is that translation done once, generically: it takes the
compose file and the images/build contexts it names, and runs the stack inside
the one container a Celaut service is given, so a multi-container application
packs and executes without anyone hand-merging its Dockerfiles first.

## How it is meant to work

The service itself runs Docker (Docker-in-Docker, or an equivalent such as
Podman-in-Podman) inside its own sandbox, and on startup runs `docker compose
up` against a compose file shipped with the packed service. The internal
compose network stays internal; whatever ports the stack needs reached from
outside are declared as this service's own API slots, in the same
`.service/service.json` shape every other repo in `celaut-basics` uses, and
proxied in to the right internal container and port.

That trade is worth being explicit about: nesting a container runtime inside
the sandbox is heavier than a native single-container service, and it is the
price of onboarding a compose app without rewriting it. A stack worth running
often enough is still a better candidate for a proper single-purpose Celaut
service, built the way `bitcoin-node` or `ergo-node` are.

## Status

This repository is at the design stage: the README states the intent so it
can be reviewed before the `.service/` packaging and the `service/` entrypoint
are written. See
[celaut-project/nodo](https://github.com/celaut-project/nodo) for the node
this is meant to run on, and the other repos in
[celaut-basics](https://github.com/celaut-basics) for the packaging shape this
one will follow once implemented.
