#!/usr/bin/env python3
"""The repo holds one pack root per architecture: `nodo pack amd64` / `nodo pack arm64`.

`nodo pack <dir>` reads `<dir>/.service/` and nothing else, takes the
architecture from `.service/service.json`, copies `<dir>` to its cache and
follows symlinks, and resolves each local dependency as `<copy>/<path>`. So
each pack root holds the dependencies of the same architecture, and the shared
sources reach it through symlinks. This is the layout of celaut-basics/
demo-service. These tests check the shape without a node.

Run with:  python3 -m unittest tests.test_layout
"""
import json
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARCHES = ("amd64", "arm64")
SERVICE_FILES = ("Dockerfile", "service.json", "pack_config.json")
# Directories that held a .service/ before the per-architecture layout.
OLD_SERVICE_DIRS = ("",)


def _json(path):
    with open(path) as fh:
        return json.load(fh)


def _pack_roots(arch):
    """(label, pack root) for the service and each dependency it packs, for `arch`."""
    root = os.path.join(ROOT, arch)
    yield "main", root
    deps = _json(os.path.join(root, ".service", "pack_config.json")).get("dependencies", {})
    for env, dep in deps.items():
        yield env, os.path.join(root, dep)


class PerArchitectureLayoutTests(unittest.TestCase):
    def test_no_service_dir_at_the_old_places(self):
        for d in OLD_SERVICE_DIRS:
            self.assertFalse(os.path.exists(os.path.join(ROOT, d, ".service")), d or ".")

    def test_each_pack_root_has_real_service_files(self):
        for arch in ARCHES:
            for label, root in _pack_roots(arch):
                for name in SERVICE_FILES:
                    path = os.path.join(os.path.realpath(root), ".service", name)
                    self.assertTrue(os.path.isfile(path), f"{arch}/{label}: {name}")
                    self.assertFalse(os.path.islink(path), f"{arch}/{label}: {name} is a symlink")

    def test_each_manifest_declares_its_directory_architecture(self):
        for arch in ARCHES:
            for label, root in _pack_roots(arch):
                declared = _json(os.path.join(root, ".service", "service.json"))["architecture"]
                self.assertEqual(declared, f"linux/{arch}", f"{arch}/{label}")

    def test_both_architectures_pack_the_same_dependencies(self):
        deps = {a: _json(os.path.join(ROOT, a, ".service", "pack_config.json")).get("dependencies", {})
                for a in ARCHES}
        self.assertEqual(deps["amd64"], deps["arm64"])

    def test_both_architectures_declare_the_same_service(self):
        # Only `architecture` can differ between the two manifests.
        specs = {}
        for a in ARCHES:
            spec = _json(os.path.join(ROOT, a, ".service", "service.json"))
            spec.pop("architecture")
            specs[a] = spec
        self.assertEqual(specs["amd64"], specs["arm64"])

    def test_dependencies_resolve_inside_the_pack_root(self):
        for arch in ARCHES:
            deps = _json(os.path.join(ROOT, arch, ".service", "pack_config.json")).get("dependencies", {})
            for env, dep in deps.items():
                self.assertFalse(os.path.isabs(dep) or ".." in dep.split("/"), f"{arch}: {env}={dep}")
                self.assertTrue(os.path.isdir(os.path.join(ROOT, arch, dep, ".service")), f"{arch}: {env}")

    def test_everything_included_exists_in_each_pack_root(self):
        for arch in ARCHES:
            for label, root in _pack_roots(arch):
                for item in _json(os.path.join(root, ".service", "pack_config.json")).get("include", []):
                    self.assertTrue(os.path.exists(os.path.join(root, item)),
                                    f"{arch}/{label}: include '{item}' is missing or a broken link")


if __name__ == "__main__":
    unittest.main(verbosity=2)
