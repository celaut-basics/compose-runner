"""The environment contract: every default, every refusal, and the boundaries.

Every value is asserted to be *refused* rather than clamped when it is nonsense, which
is the decision `service/config.py` is built around: a `COMPOSE_UP_TIMEOUT_S=abc`
silently treated as 600 is a service whose declared limit is not the limit it enforces.
"""

import unittest

import config
from config import ConfigError


class Defaults(unittest.TestCase):
    def setUp(self):
        self.loaded = config.load({})

    def test_the_compose_file_defaults_to_the_packed_stack(self):
        self.assertEqual("/app/stack/docker-compose.yml", self.loaded.compose_file)

    def test_the_project_name_defaults_to_stack(self):
        self.assertEqual("stack", self.loaded.project_name)

    def test_the_up_timeout_defaults_to_ten_minutes(self):
        # An image pull on a cold instance is the dominant term, and a stack of three or
        # four images routinely exceeds a minute.
        self.assertEqual(600, self.loaded.up_timeout_s)

    def test_the_dockerd_timeout_is_separate_from_the_compose_one(self):
        # Separate because the two failures are different, and an operator reading a log
        # should be able to tell "the runtime never came up" from "the stack never did".
        self.assertEqual(60, self.loaded.dockerd_timeout_s)

    def test_the_down_timeout_defaults_below_a_typical_kill_grace(self):
        self.assertEqual(30, self.loaded.down_timeout_s)

    def test_the_storage_driver_defaults_to_auto(self):
        self.assertEqual("auto", self.loaded.storage_driver)

    def test_the_data_root_is_on_the_rootfs_and_not_in_tmp(self):
        # The single most consequential default in the service. /tmp is a tmpfs under
        # nodo's initramfs, so a data-root there would charge every image layer against
        # the instance's MEMORY limit rather than its disk.
        self.assertEqual("/var/lib/docker", self.loaded.data_root)
        self.assertFalse(self.loaded.data_root.startswith("/tmp"))

    def test_the_health_port_defaults_to_9000(self):
        self.assertEqual(9000, self.loaded.health_port)


class ComposeFile(unittest.TestCase):
    def test_an_absolute_path_is_taken(self):
        self.assertEqual("/app/other.yml", config.load({"COMPOSE_FILE": "/app/other.yml"}).compose_file)

    def test_a_relative_path_is_refused(self):
        # It would resolve against whatever the supervisor's cwd happens to be, which is
        # not something a service's contract should depend on.
        with self.assertRaises(ConfigError) as caught:
            config.load({"COMPOSE_FILE": "stack/docker-compose.yml"})
        self.assertIn("absolute", str(caught.exception))

    def test_an_empty_value_falls_back_to_the_default(self):
        self.assertEqual(config.DEFAULT_COMPOSE_FILE, config.load({"COMPOSE_FILE": ""}).compose_file)

    def test_several_files_are_refused_rather_than_taking_the_first(self):
        # compose's own COMPOSE_FILE supports a path-separated list. Taking the first
        # silently would mean the cross-check validated a different document than the one
        # that runs.
        with self.assertRaises(ConfigError) as caught:
            config.load({"COMPOSE_FILE": "/app/a.yml:/app/b.yml"})
        self.assertIn("more than one", str(caught.exception))

    def test_a_comma_separated_list_is_refused_too(self):
        with self.assertRaises(ConfigError):
            config.load({"COMPOSE_FILE": "/app/a.yml,/app/b.yml"})


class ProjectName(unittest.TestCase):
    def test_a_valid_name_is_taken(self):
        self.assertEqual("my-stack1", config.load({"COMPOSE_PROJECT_NAME": "my-stack1"}).project_name)

    def test_underscores_are_allowed(self):
        self.assertEqual("my_stack", config.load({"COMPOSE_PROJECT_NAME": "my_stack"}).project_name)

    def test_an_uppercase_name_is_refused_naming_composes_rule(self):
        # compose refuses this itself rather than normalising it, so refusing here -- at
        # start, with the rule in the message -- is strictly better than the same
        # rejection arriving mid-launch.
        with self.assertRaises(ConfigError) as caught:
            config.load({"COMPOSE_PROJECT_NAME": "MyStack"})
        self.assertIn("lowercase", str(caught.exception))

    def test_a_leading_hyphen_is_refused(self):
        with self.assertRaises(ConfigError):
            config.load({"COMPOSE_PROJECT_NAME": "-stack"})

    def test_a_name_with_a_slash_is_refused(self):
        with self.assertRaises(ConfigError):
            config.load({"COMPOSE_PROJECT_NAME": "my/stack"})

    def test_a_name_with_a_space_is_refused(self):
        with self.assertRaises(ConfigError):
            config.load({"COMPOSE_PROJECT_NAME": "my stack"})

    def test_an_empty_name_falls_back_to_the_default(self):
        self.assertEqual("stack", config.load({"COMPOSE_PROJECT_NAME": "  "}).project_name)


class Timeouts(unittest.TestCase):
    def test_a_number_is_taken(self):
        self.assertEqual(90, config.load({"COMPOSE_UP_TIMEOUT_S": "90"}).up_timeout_s)

    def test_a_non_number_is_refused_not_defaulted(self):
        with self.assertRaises(ConfigError) as caught:
            config.load({"COMPOSE_UP_TIMEOUT_S": "abc"})
        self.assertIn("whole number", str(caught.exception))

    def test_a_signed_number_is_refused(self):
        # int() would accept "+300"; a port or a timeout written that way is much more
        # likely a mistake than an intention.
        with self.assertRaises(ConfigError):
            config.load({"COMPOSE_UP_TIMEOUT_S": "+300"})

    def test_an_underscored_number_is_refused(self):
        # int("1_0") is 10 in Python. Not here.
        with self.assertRaises(ConfigError):
            config.load({"COMPOSE_UP_TIMEOUT_S": "1_0"})

    def test_a_negative_number_is_refused(self):
        with self.assertRaises(ConfigError):
            config.load({"COMPOSE_UP_TIMEOUT_S": "-1"})

    def test_zero_is_below_the_floor(self):
        with self.assertRaises(ConfigError):
            config.load({"COMPOSE_UP_TIMEOUT_S": "0"})

    def test_a_value_over_the_ceiling_is_refused(self):
        with self.assertRaises(ConfigError) as caught:
            config.load({"COMPOSE_UP_TIMEOUT_S": "999999"})
        self.assertIn("range", str(caught.exception))

    def test_an_empty_value_falls_back_to_the_default(self):
        self.assertEqual(600, config.load({"COMPOSE_UP_TIMEOUT_S": ""}).up_timeout_s)

    def test_the_boundaries_are_accepted(self):
        self.assertEqual(5, config.load({"COMPOSE_UP_TIMEOUT_S": "5"}).up_timeout_s)
        self.assertEqual(7200, config.load({"COMPOSE_UP_TIMEOUT_S": "7200"}).up_timeout_s)

    def test_the_dockerd_timeout_is_read_separately(self):
        loaded = config.load({"DOCKERD_TIMEOUT_S": "120", "COMPOSE_UP_TIMEOUT_S": "60"})
        self.assertEqual(120, loaded.dockerd_timeout_s)
        self.assertEqual(60, loaded.up_timeout_s)

    def test_the_down_timeout_is_read(self):
        self.assertEqual(5, config.load({"COMPOSE_DOWN_TIMEOUT_S": "5"}).down_timeout_s)


class StorageDriver(unittest.TestCase):
    def test_overlay2_is_accepted(self):
        self.assertEqual("overlay2", config.load({"DOCKERD_STORAGE_DRIVER": "overlay2"}).storage_driver)

    def test_vfs_is_accepted(self):
        self.assertEqual("vfs", config.load({"DOCKERD_STORAGE_DRIVER": "vfs"}).storage_driver)

    def test_case_is_normalised(self):
        self.assertEqual("vfs", config.load({"DOCKERD_STORAGE_DRIVER": "VFS"}).storage_driver)

    def test_a_driver_this_service_does_not_ship_is_refused(self):
        # btrfs and zfs need kernel support the guest kernel does not have
        # (CONFIG_BTRFS_FS is explicitly off in nodo-guest.config), so accepting the name
        # would produce a dockerd that fails to start with a driver error.
        with self.assertRaises(ConfigError) as caught:
            config.load({"DOCKERD_STORAGE_DRIVER": "btrfs"})
        self.assertIn("overlay2", str(caught.exception))

    def test_an_empty_value_is_auto(self):
        self.assertEqual("auto", config.load({"DOCKERD_STORAGE_DRIVER": ""}).storage_driver)


class DataRoot(unittest.TestCase):
    def test_an_absolute_path_is_taken(self):
        self.assertEqual("/data/docker", config.load({"DOCKERD_DATA_ROOT": "/data/docker"}).data_root)

    def test_a_relative_path_is_refused(self):
        with self.assertRaises(ConfigError):
            config.load({"DOCKERD_DATA_ROOT": "var/lib/docker"})


class HealthPort(unittest.TestCase):
    def test_a_port_is_taken(self):
        self.assertEqual(9999, config.load({"HEALTH_PORT": "9999"}).health_port)

    def test_zero_is_refused(self):
        with self.assertRaises(ConfigError):
            config.load({"HEALTH_PORT": "0"})

    def test_a_port_over_65535_is_refused(self):
        with self.assertRaises(ConfigError):
            config.load({"HEALTH_PORT": "65536"})

    def test_the_default_matches_what_the_declaration_and_preflight_assume(self):
        # Three places know this number: config.py's default, .service/service.json's
        # second slot, and .service/preflight.py's exemption. If they drift, every build
        # fails with the health slot reported as a port nothing publishes -- so they are
        # asserted equal here rather than left to be discovered that way.
        import json
        import os
        import sys

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sys.path.insert(0, os.path.join(root, ".service"))
        import preflight

        with open(os.path.join(root, ".service", "service.json"), encoding="utf-8") as handle:
            declaration = json.load(handle)

        self.assertEqual(config.DEFAULT_HEALTH_PORT, preflight.health_port(declaration))
        slot_ports = {slot["port"] for slot in declaration["api"]}
        self.assertIn(config.DEFAULT_HEALTH_PORT, slot_ports)


class TheDeclarationListsEveryVariable(unittest.TestCase):
    def test_every_env_read_by_config_is_declared_in_service_json(self):
        """`envs` in service.json is the contract, so it has to be complete.

        An operator reads that list to know what they may set. A variable this service
        honours and does not declare is a feature nobody can discover; one it declares
        and ignores is a promise it does not keep.
        """
        import json
        import os

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, ".service", "service.json"), encoding="utf-8") as handle:
            declared = set(json.load(handle)["envs"])

        read_by_config = {
            "COMPOSE_PROJECT_NAME",
            "COMPOSE_FILE",
            "COMPOSE_UP_TIMEOUT_S",
            "COMPOSE_DOWN_TIMEOUT_S",
            "DOCKERD_TIMEOUT_S",
            "DOCKERD_STORAGE_DRIVER",
            "DOCKERD_DATA_ROOT",
            "HEALTH_PORT",
        }
        # IPTABLES_BACKEND is read by entrypoint.sh rather than config.py, and is
        # declared; assert it explicitly so the shell's variable is not forgotten.
        self.assertIn("IPTABLES_BACKEND", declared)
        self.assertEqual(set(), read_by_config - declared, "read but not declared")


if __name__ == "__main__":
    unittest.main()
