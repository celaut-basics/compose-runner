"""The repo against the packer of the current nodo, without a node.

`nodo pack` does not build the repo as it is on disk. It builds a context made by
`prepare_directory` and `generate_service_zip` (nodo
`src/commands/packer/zip_with_dockerfile/`):

1. every `COPY ./x` in `<arch>/.service/Dockerfile` becomes `COPY service/x`
   (`--from=` lines are not changed);
2. the build context is `.service/`, and `.service/service/` holds only the items
   that `include` in `pack_config.json` lists;
3. the patterns of `ignore`, plus the lines of `.dockerignore`, are removed from that
   copy recursively (`rglob`).

A COPY source that is not in that context fails the pack, after the operator waits
for a packer. These tests make the same context in a temporary directory and check
every COPY source against it. They also check the parts of the service contract that
the packer reads (`init.entry_path`, `architecture`, `envs`) and the parts of the
Dockerfile that nodo ignores (`ENV`, `ENTRYPOINT`, `CMD`).
"""

import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

import config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# One pack root per architecture (`nodo pack amd64` / `nodo pack arm64`). Each holds
# its own `.service/` and links to the shared sources (see test_layout.py).
ARCHES = ("amd64", "arm64")


def _service_dir(arch):
    return os.path.join(ROOT, arch, ".service")


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _dockerfile_lines(arch):
    """The Dockerfile as logical lines: continuations joined, comments removed."""
    logical, current = [], ""
    for raw in _read(os.path.join(_service_dir(arch), "Dockerfile")).splitlines():
        if not current and raw.lstrip().startswith("#"):
            continue
        if raw.rstrip().endswith("\\"):
            current += raw.rstrip()[:-1] + " "
            continue
        current += raw
        if current.strip():
            logical.append(current.strip())
        current = ""
    return logical


def _rewrite_copy(line):
    """nodo's `__dockerfile_copy_from`, for one line."""
    if not line.startswith("COPY "):
        return line
    parts = line.split()
    flags = [p for p in parts[1:] if p.startswith("--")]
    if len(parts) >= 3 and not any(f.startswith("--from") for f in flags):
        start = 1
        while start < len(parts) and parts[start].startswith("--"):
            start += 1
        for j in range(start, len(parts) - 1):
            if parts[j].startswith("."):
                parts[j] = "service" + parts[j][1:]
    return " ".join(parts)


def _build_context(destination, arch):
    """The `.service/` build context that nodo's packer makes from the `arch` pack root.

    nodo copies the pack root and follows symlinks, so do the copies here.
    """
    pack_root = os.path.join(ROOT, arch)
    service_dir = _service_dir(arch)
    context = os.path.join(destination, ".service")
    shutil.copytree(service_dir, context)
    pack_config = json.loads(_read(os.path.join(service_dir, "pack_config.json")))

    ignore = list(pack_config.get("ignore", []))
    for candidate in (os.path.join(service_dir, ".dockerignore"), os.path.join(pack_root, ".dockerignore")):
        if os.path.exists(candidate):
            ignore.extend(_read(candidate).splitlines())
            break

    source = os.path.join(context, "service")
    os.makedirs(source)
    for item in pack_config["include"]:
        src = os.path.join(pack_root, item)
        dest = os.path.join(source, item)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.isdir(src):
            shutil.copytree(src, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dest)

    for pattern in ignore:
        pattern = pattern.strip()
        if not pattern or pattern.startswith(("#", "!")):
            continue
        pattern = pattern.rstrip("/")
        if not pattern:
            continue
        for path in pathlib.Path(source).rglob(pattern):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
    return context


class CopySources(unittest.TestCase):
    def test_every_copy_source_exists_in_the_context_nodo_builds(self):
        for arch in ARCHES:
            with self.subTest(arch=arch):
                self._check_copy_sources(arch)

    def _check_copy_sources(self, arch):
        copies = [
            _rewrite_copy(line)
            for line in _dockerfile_lines(arch)
            if line.startswith("COPY ") and "--from=" not in line
        ]
        self.assertTrue(copies, "the Dockerfile has no COPY from the context")
        with tempfile.TemporaryDirectory() as directory:
            context = _build_context(directory, arch)
            for line in copies:
                for source in line.split()[1:-1]:
                    if source.startswith("--"):
                        continue
                    with self.subTest(copy=line):
                        self.assertTrue(
                            os.path.exists(os.path.join(context, source)),
                            f"`{line}` has no source in the packer's context. "
                            "Add the path to `include` in pack_config.json.",
                        )

    def test_every_copy_source_is_written_with_a_dot_prefix(self):
        # PACKING.md: a bare relative origin (`COPY src /app`) is not rewritten, and it
        # then reads the `.service/` context, not the project.
        for line in (l for arch in ARCHES for l in _dockerfile_lines(arch)):
            if line.startswith("COPY ") and "--from=" not in line:
                with self.subTest(copy=line):
                    for source in line.split()[1:-1]:
                        if not source.startswith("--"):
                            self.assertTrue(source.startswith("./"), line)

    def test_the_ignore_patterns_keep_the_files_the_image_needs(self):
        # nodo applies `ignore` and `.dockerignore` recursively, so a pattern such as
        # `*.md` or `tests/` also removes files inside an operator's build contexts.
        for arch in ARCHES:
            with tempfile.TemporaryDirectory() as directory:
                self._check_kept(_build_context(directory, arch))

    def _check_kept(self, context):
            for path in (
                "service/service/entrypoint.sh",
                "service/service/supervisor.py",
                "service/stack/docker-compose.yml",
                "service/.service/service.json",
                "service/.service/preflight.py",
            ):
                with self.subTest(path=path):
                    self.assertTrue(os.path.exists(os.path.join(context, path)))

    def test_no_ignore_pattern_is_one_that_rglob_cannot_take(self):
        # pathlib refuses an absolute pattern ("/tests"), and the pack would stop
        # with NotImplementedError.
        patterns = []
        for arch in ARCHES:
            pack_config = json.loads(_read(os.path.join(_service_dir(arch), "pack_config.json")))
            patterns += pack_config.get("ignore", [])
        patterns += _read(os.path.join(ROOT, ".dockerignore")).splitlines()
        for pattern in patterns:
            self.assertFalse(pattern.strip().startswith("/"), pattern)


class DockerfileRules(unittest.TestCase):
    def test_no_entrypoint_and_no_cmd(self):
        # nodo ignores both; init.entry_path in service.json is what runs.
        for line in (l for arch in ARCHES for l in _dockerfile_lines(arch)):
            self.assertFalse(re.match(r"^(ENTRYPOINT|CMD)\b", line), line)

    def test_both_architectures_have_pinned_checksums(self):
        for arch in ARCHES:
            self._check_checksums(_read(os.path.join(_service_dir(arch), "Dockerfile")))

    def _check_checksums(self, text):
        for name in (
            "DOCKER_SHA256_ARM64",
            "DOCKER_SHA256_AMD64",
            "COMPOSE_SHA256_ARM64",
            "COMPOSE_SHA256_AMD64",
        ):
            with self.subTest(arg=name):
                self.assertRegex(text, rf"ARG {name}=[0-9a-f]{{64}}\n")

    def test_each_tree_declares_its_own_architecture(self):
        for arch in ARCHES:
            declaration = json.loads(_read(os.path.join(_service_dir(arch), "service.json")))
            self.assertEqual(f"linux/{arch}", declaration["architecture"])


class EntryPath(unittest.TestCase):
    def test_the_entry_path_is_the_entrypoint_script(self):
        for arch in ARCHES:
            declaration = json.loads(_read(os.path.join(_service_dir(arch), "service.json")))
            self.assertEqual(["service", "entrypoint.sh"], declaration["init"]["entry_path"])

    def test_the_entrypoint_is_executable_in_the_repo(self):
        # The packer keeps the mode bits, and the Dockerfile also sets 0755.
        mode = os.stat(os.path.join(ROOT, "service", "entrypoint.sh")).st_mode
        self.assertTrue(mode & stat.S_IXUSR)

    def test_the_entrypoint_is_valid_posix_sh(self):
        result = subprocess.run(
            ["sh", "-n", os.path.join(ROOT, "service", "entrypoint.sh")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_the_entrypoint_sets_the_same_path_as_the_supervisor(self):
        # nodo ignores `ENV PATH` in the Dockerfile. The entrypoint is what sets it.
        text = _read(os.path.join(ROOT, "service", "entrypoint.sh"))
        match = re.search(r"^PATH=(\S+)$", text, re.MULTILINE)
        self.assertIsNotNone(match, "entrypoint.sh does not set PATH")
        self.assertEqual(config.RUNTIME_PATH, match.group(1))
        self.assertRegex(text, r"(?m)^export PATH$")
        self.assertIn("/opt/docker/bin", config.RUNTIME_PATH.split(":"))
        self.assertEqual("/usr/local/sbin", config.RUNTIME_PATH.split(":")[0])

    def test_the_entrypoint_moves_to_cgroup_v1_only_with_no_bpf(self):
        # NODE-REQUIREMENTS.md finding 8 b: no bpf(2) means no device rules on cgroup v2.
        # The switch must key on the same sysctl as the supervisor's warning, keep v2
        # when bpf(2) exists, and put v2 back if the v1 devices controller is missing.
        text = _read(os.path.join(ROOT, "service", "entrypoint.sh"))
        self.assertIn(config.BPF_SYSCTL, text)
        self.assertRegex(
            text,
            r'(?m)^if \[ -f /sys/fs/cgroup/cgroup\.controllers \] && \[ ! -e '
            + re.escape(config.BPF_SYSCTL) + r' \]; then\n    cgroup_v1_for_devices\nfi$',
        )
        self.assertIn('*" devices "*)', text)
        self.assertIn("mount -t cgroup2 none /sys/fs/cgroup ||", text)

    def test_the_supervisor_runs_under_tini(self):
        text = _read(os.path.join(ROOT, "service", "entrypoint.sh"))
        self.assertRegex(
            text,
            r"(?m)^exec /opt/docker/bin/docker-init -- /usr/bin/python3 /service/supervisor\.py$",
        )
        self.assertEqual("/opt/docker/bin/docker-init", config.DOCKER_INIT_BIN)


class Resources(unittest.TestCase):
    def test_at_most_disk_is_not_above_at_init_disk(self):
        # The rootfs is sized once, from at_init.disk_space. A larger at_most figure
        # grows nothing and only raises the admission quote.
        for arch in ARCHES:
            resources = json.loads(_read(os.path.join(_service_dir(arch), "service.json")))["resources"]
            self.assertLessEqual(
                resources["at_most"]["disk_space"], resources["at_init"]["disk_space"]
            )


if __name__ == "__main__":
    unittest.main()
