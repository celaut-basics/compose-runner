"""The supervisor's decisions, with every subprocess replaced.

No Docker, no network, no privileges. What is being tested is the sequencing and the
failure handling -- which storage driver is tried second, that a failed `up` tears down
what started, that SIGTERM becomes `compose down`, that the argv is a list and never a
shell string -- because those are the parts that are wrong in a way an image test does
not notice.
"""

import os
import signal
import subprocess
import unittest

import compose_spec
import config
import supervisor
from supervisor import StackError, Supervisor


def make_config(**overrides):
    values = {
        "project_name": "stack",
        "compose_file": "/app/stack/docker-compose.yml",
        "up_timeout_s": 600,
        "dockerd_timeout_s": 60,
        "storage_driver": "auto",
        "data_root": "/var/lib/docker",
        "health_port": 9000,
        "down_timeout_s": 30,
    }
    values.update(overrides)
    return config.Config(**values)


class FakeProcess:
    """A stand-in for a Popen: alive until told otherwise."""

    def __init__(self, returncode=None):
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        _ = timeout
        return self.returncode


class ComposeArgv(unittest.TestCase):
    def setUp(self):
        self.supervisor = Supervisor(make_config())

    def test_the_compose_file_and_project_are_named_explicitly(self):
        argv = self.supervisor.compose_argv("up", "--detach")
        self.assertIn("--file", argv)
        self.assertIn("/app/stack/docker-compose.yml", argv)
        self.assertIn("--project-name", argv)
        self.assertIn("stack", argv)

    def test_the_project_directory_is_the_compose_files_own(self):
        # So relative `build.context` paths in the packed stack resolve against the
        # stack, not against whatever cwd this process happens to have.
        argv = self.supervisor.compose_argv("up")
        self.assertEqual("/app/stack", argv[argv.index("--project-directory") + 1])

    def test_it_is_a_list_and_every_element_is_a_string(self):
        # The compose file path and project name come from the environment. A string
        # command would make either of them a place to write `; rm -rf /`.
        argv = self.supervisor.compose_argv("ps", "--format", "json")
        self.assertIsInstance(argv, list)
        for element in argv:
            self.assertIsInstance(element, str)

    def test_the_docker_binary_is_the_pinned_absolute_path(self):
        self.assertEqual(config.DOCKER_BIN, self.supervisor.compose_argv("ps")[0])


class DockerdArgv(unittest.TestCase):
    def setUp(self):
        self.supervisor = Supervisor(make_config())

    def test_the_socket_is_a_unix_socket_and_not_a_tcp_port(self):
        # A dockerd listening on TCP would be root on this instance for anyone who
        # reached it.
        argv = self.supervisor.dockerd_argv("overlay2")
        host = argv[argv.index("--host") + 1]
        self.assertTrue(host.startswith("unix://"))
        self.assertFalse(any(element.startswith("tcp://") for element in argv))

    def test_the_socket_is_on_the_run_tmpfs(self):
        self.assertEqual("/run/docker.sock", self.supervisor.socket_path())

    def test_the_data_root_is_passed_and_is_not_under_tmp(self):
        # /tmp is a tmpfs under nodo's initramfs: image layers there are charged against
        # memory, not disk.
        argv = self.supervisor.dockerd_argv("overlay2")
        data_root = argv[argv.index("--data-root") + 1]
        self.assertEqual("/var/lib/docker", data_root)
        self.assertFalse(data_root.startswith("/tmp"))

    def test_the_storage_driver_is_the_one_asked_for(self):
        argv = self.supervisor.dockerd_argv("vfs")
        self.assertEqual("vfs", argv[argv.index("--storage-driver") + 1])

    def test_iptables_management_is_left_on(self):
        # Turning it off would break egress for every container in the stack, since the
        # internal bridge needs NAT to reach anything outside the VM.
        self.assertIn("--iptables=true", self.supervisor.dockerd_argv("overlay2"))

    def test_live_restore_is_off(self):
        # The VM is the lifetime. A daemon keeping containers across its own restart
        # would keep them past the supervisor that owns them.
        self.assertIn("--live-restore=false", self.supervisor.dockerd_argv("overlay2"))


class ComposeEnvironment(unittest.TestCase):
    def setUp(self):
        self.supervisor = Supervisor(make_config())

    def test_docker_host_points_at_the_daemon_this_process_started(self):
        env = self.supervisor.compose_env()
        self.assertEqual("unix:///run/docker.sock", env["DOCKER_HOST"])

    def test_an_inherited_docker_host_is_overridden(self):
        # Otherwise an inherited value could redirect the whole stack onto another
        # daemon.
        previous = os.environ.get("DOCKER_HOST")
        os.environ["DOCKER_HOST"] = "tcp://evil.example:2375"
        try:
            self.assertEqual("unix:///run/docker.sock", self.supervisor.compose_env()["DOCKER_HOST"])
        finally:
            if previous is None:
                del os.environ["DOCKER_HOST"]
            else:
                os.environ["DOCKER_HOST"] = previous

    def test_the_environment_is_inherited_rather_than_replaced(self):
        # Deliberate, and the opposite of what yt-transcript does: a compose file's
        # `${VAR}` interpolation is a documented compose feature, and an operator packing
        # a stack that reads ${POSTGRES_PASSWORD} from stack/.env is doing the ordinary
        # thing. A replaced environment would interpolate those to empty strings.
        previous = os.environ.get("COMPOSE_RUNNER_TEST_VAR")
        os.environ["COMPOSE_RUNNER_TEST_VAR"] = "kept"
        try:
            self.assertEqual("kept", self.supervisor.compose_env()["COMPOSE_RUNNER_TEST_VAR"])
        finally:
            if previous is None:
                del os.environ["COMPOSE_RUNNER_TEST_VAR"]
            else:
                os.environ["COMPOSE_RUNNER_TEST_VAR"] = previous

    def test_the_project_name_is_exported_for_compose(self):
        self.assertEqual("stack", self.supervisor.compose_env()["COMPOSE_PROJECT_NAME"])

    def test_progress_output_is_plain_because_the_console_is_not_a_terminal(self):
        self.assertEqual("plain", self.supervisor.compose_env()["COMPOSE_PROGRESS"])


class StorageDriverFallback(unittest.TestCase):
    """The overlay2 -> vfs fallback, which is the one runtime adaptation this makes."""

    def setUp(self):
        self.supervisor = Supervisor(make_config())
        self.attempts = []

    def _patch(self, succeed_on):
        def fake_popen(argv, env=None):
            _ = env
            driver = argv[argv.index("--storage-driver") + 1]
            self.attempts.append(driver)
            return FakeProcess()

        def fake_wait():
            return (self.attempts[-1] == succeed_on), "overlay2 not supported"

        self.supervisor.wait_for_docker = fake_wait
        # The directories are the one part of the start path that needs privileges on a
        # real filesystem, and this test is about which driver is tried second.
        self.supervisor.prepare_directories = lambda: None
        supervisor.subprocess.Popen = fake_popen

    def tearDown(self):
        supervisor.subprocess.Popen = subprocess.Popen

    def test_overlay2_is_tried_first(self):
        self._patch(succeed_on="overlay2")
        self.supervisor.start_dockerd()
        self.assertEqual(["overlay2"], self.attempts)
        self.assertEqual("overlay2", self.supervisor.storage_driver_used)

    def test_vfs_is_tried_when_overlay2_does_not_come_up(self):
        # Which happens for real: an overlayfs upper dir on top of another overlayfs is
        # refused by the kernel, and a read_only_filesystem service gets exactly that.
        self._patch(succeed_on="vfs")
        self.supervisor.start_dockerd()
        self.assertEqual(["overlay2", "vfs"], self.attempts)
        self.assertEqual("vfs", self.supervisor.storage_driver_used)

    def test_both_failing_is_a_stack_error_naming_both(self):
        self._patch(succeed_on="neither")
        with self.assertRaises(StackError) as caught:
            self.supervisor.start_dockerd()
        message = str(caught.exception)
        self.assertIn("overlay2", message)
        self.assertIn("vfs", message)

    def test_an_explicit_driver_is_not_fallen_back_from(self):
        # If an operator said vfs, trying overlay2 would be ignoring them.
        self.supervisor = Supervisor(make_config(storage_driver="vfs"))
        self._patch(succeed_on="vfs")
        self.supervisor.start_dockerd()
        self.assertEqual(["vfs"], self.attempts)

    def test_an_explicit_driver_that_fails_does_not_try_another(self):
        self.supervisor = Supervisor(make_config(storage_driver="overlay2"))
        self._patch(succeed_on="neither")
        with self.assertRaises(StackError):
            self.supervisor.start_dockerd()
        self.assertEqual(["overlay2"], self.attempts)


class Signals(unittest.TestCase):
    def setUp(self):
        self.supervisor = Supervisor(make_config())

    def test_a_sigterm_sets_the_stopping_flag(self):
        self.assertFalse(self.supervisor.stopping)
        self.supervisor._on_signal(signal.SIGTERM, None)
        self.assertTrue(self.supervisor.stopping)

    def test_a_second_signal_while_stopping_is_ignored(self):
        # Otherwise an impatient `docker stop` sending two TERMs would start two
        # teardowns.
        self.supervisor._on_signal(signal.SIGTERM, None)
        self.supervisor._on_signal(signal.SIGTERM, None)
        self.assertTrue(self.supervisor.stopping)

    def test_the_supervise_loop_returns_zero_when_signalled(self):
        self.supervisor.stopping = True
        self.assertEqual(0, self.supervisor.supervise())

    def test_the_supervise_loop_returns_one_if_dockerd_dies(self):
        # A dockerd that exited while the stack was meant to be running is a fatal
        # condition the node should hear about as a non-zero exit.
        self.supervisor.dockerd = FakeProcess(returncode=1)
        self.assertEqual(1, self.supervisor.supervise())


class Reaping(unittest.TestCase):
    def test_reap_returns_zero_when_there_is_nothing_to_reap(self):
        # ECHILD is the ordinary answer, not an error: a PID 1 with no children must not
        # treat "nothing to reap" as a failure.
        self.assertEqual(0, Supervisor(make_config()).reap())

    def test_reap_counts_an_exited_child(self):
        # A real forked child, reaped through the same path PID 1 would use for an
        # orphaned docker-proxy.
        sup = Supervisor(make_config())
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        # The child may not have exited yet; reap until it is gone or we give up.
        import time

        reaped = 0
        deadline = time.monotonic() + 5
        while reaped == 0 and time.monotonic() < deadline:
            reaped = sup.reap()
            if reaped == 0:
                time.sleep(0.05)
        self.assertEqual(1, reaped)


class Validation(unittest.TestCase):
    def setUp(self):
        self.supervisor = Supervisor(make_config())

    def test_a_matching_stack_and_declaration_validate(self):
        self.supervisor.read_compose_document = lambda: {
            "services": {
                "w": {"image": "x", "ports": [{"target": 80, "published": "8080", "protocol": "tcp"}]}
            }
        }
        self.supervisor.read_service_json = lambda: {"api": [{"port": 8080}, {"port": 9000}]}
        document = self.supervisor.validate()
        self.assertEqual(["w"], compose_spec.service_names(document))

    def test_a_mismatch_is_raised_before_anything_starts(self):
        self.supervisor.read_compose_document = lambda: {
            "services": {
                "w": {"image": "x", "ports": [{"target": 80, "published": "9999", "protocol": "tcp"}]}
            }
        }
        self.supervisor.read_service_json = lambda: {"api": [{"port": 8080}]}
        with self.assertRaises(compose_spec.ComposeSpecError):
            self.supervisor.validate()

    def test_the_expected_service_count_is_recorded_for_the_health_slot(self):
        # Without it, a stack that started two of three containers reads as ready.
        self.supervisor.read_compose_document = lambda: {
            "services": {"a": {"image": "x"}, "b": {"image": "y"}, "c": {"image": "z"}}
        }
        self.supervisor.read_service_json = lambda: {}
        self.supervisor.validate()
        self.assertEqual(3, self.supervisor._expected_services)

    def test_a_declaration_with_no_api_skips_the_cross_check(self):
        self.supervisor.read_compose_document = lambda: {"services": {"a": {"image": "x"}}}
        self.supervisor.read_service_json = lambda: {}
        self.supervisor.validate()  # must not raise

    def test_a_missing_service_json_is_tolerated_and_a_malformed_one_is_not(self):
        # Absent is the shape of a hand-run `docker run`. Malformed is a broken
        # declaration, and reading past it would skip the check entirely.
        self.supervisor.read_compose_document = lambda: {"services": {"a": {"image": "x"}}}
        original = config.SERVICE_JSON
        try:
            config.SERVICE_JSON = "/nonexistent/service.json"
            supervisor.SERVICE_JSON = "/nonexistent/service.json"
            self.assertEqual({}, self.supervisor.read_service_json())
        finally:
            config.SERVICE_JSON = original
            supervisor.SERVICE_JSON = original


class TearDown(unittest.TestCase):
    def test_compose_down_names_remove_orphans_and_a_timeout(self):
        sup = Supervisor(make_config(down_timeout_s=7))
        seen = {}

        def fake_run(argv, timeout_s, env=None, cwd=None):
            seen["argv"] = argv
            seen["timeout_s"] = timeout_s
            return 0, "", ""

        supervisor._run = fake_run
        try:
            self.assertEqual(0, sup.compose_down())
        finally:
            supervisor._run = _original_run
        self.assertIn("down", seen["argv"])
        self.assertIn("--remove-orphans", seen["argv"])
        self.assertEqual("7", seen["argv"][seen["argv"].index("--timeout") + 1])
        # The subprocess budget exceeds the container grace, so down's own SIGKILL
        # escalation happens inside the call rather than being cut off by it.
        self.assertGreater(seen["timeout_s"], 7)

    def test_a_failed_down_is_reported_in_the_exit_status(self):
        sup = Supervisor(make_config())
        supervisor._run = lambda argv, timeout_s, env=None, cwd=None: (1, "", "could not stop")
        try:
            self.assertEqual(1, sup.compose_down())
        finally:
            supervisor._run = _original_run

    def test_stop_dockerd_terminates_then_kills(self):
        sup = Supervisor(make_config())
        process = FakeProcess()
        sup.dockerd = process
        sup.stop_dockerd()
        self.assertTrue(process.terminated)
        self.assertIsNone(sup.dockerd)

    def test_stop_dockerd_on_an_already_dead_daemon_is_a_no_op(self):
        sup = Supervisor(make_config())
        sup.dockerd = FakeProcess(returncode=0)
        sup.stop_dockerd()
        self.assertIsNone(sup.dockerd)


class OfflineImages(unittest.TestCase):
    def test_no_images_directory_loads_nothing(self):
        sup = Supervisor(make_config())
        original = supervisor.STACK_DIR
        try:
            supervisor.STACK_DIR = "/nonexistent/stack"
            self.assertEqual(0, sup.load_offline_images())
        finally:
            supervisor.STACK_DIR = original

    def test_tars_are_loaded_in_sorted_order(self):
        import tempfile

        sup = Supervisor(make_config())
        loaded = []

        def fake_run(argv, timeout_s, env=None, cwd=None):
            loaded.append(argv[argv.index("--input") + 1])
            return 0, "Loaded image: x", ""

        with tempfile.TemporaryDirectory() as directory:
            images = os.path.join(directory, "images")
            os.makedirs(images)
            for name in ("b.tar", "a.tar", "notes.txt"):
                open(os.path.join(images, name), "w").close()
            original_dir, original_run = supervisor.STACK_DIR, supervisor._run
            try:
                supervisor.STACK_DIR = directory
                supervisor._run = fake_run
                self.assertEqual(2, sup.load_offline_images())
            finally:
                supervisor.STACK_DIR = original_dir
                supervisor._run = original_run

        self.assertEqual(["a.tar", "b.tar"], [os.path.basename(p) for p in loaded])

    def test_a_tar_that_fails_to_load_is_fatal(self):
        # An image the operator packed and this service silently skipped would fail later
        # as a pull against a registry they may have declared no egress to.
        import tempfile

        sup = Supervisor(make_config())
        with tempfile.TemporaryDirectory() as directory:
            images = os.path.join(directory, "images")
            os.makedirs(images)
            open(os.path.join(images, "a.tar"), "w").close()
            original_dir, original_run = supervisor.STACK_DIR, supervisor._run
            try:
                supervisor.STACK_DIR = directory
                supervisor._run = lambda argv, timeout_s, env=None, cwd=None: (1, "", "invalid tar")
                with self.assertRaises(StackError) as caught:
                    sup.load_offline_images()
                self.assertIn("invalid tar", str(caught.exception))
            finally:
                supervisor.STACK_DIR = original_dir
                supervisor._run = original_run


class RunHelper(unittest.TestCase):
    def test_a_timeout_is_reported_as_124_with_the_command(self):
        code, _, stderr = supervisor._run(["sleep", "5"], timeout_s=1)
        self.assertEqual(124, code)
        self.assertIn("timed out", stderr)

    def test_a_missing_binary_is_reported_as_127_and_not_raised(self):
        code, _, stderr = supervisor._run(["/nonexistent/binary"], timeout_s=5)
        self.assertEqual(127, code)
        self.assertIn("cannot execute", stderr)

    def test_output_is_captured_as_text(self):
        code, stdout, _ = supervisor._run(["echo", "hello"], timeout_s=5)
        self.assertEqual(0, code)
        self.assertEqual("hello", stdout.strip())


class Main(unittest.TestCase):
    def test_a_bad_environment_exits_two_without_starting_anything(self):
        previous = os.environ.get("COMPOSE_UP_TIMEOUT_S")
        os.environ["COMPOSE_UP_TIMEOUT_S"] = "not-a-number"
        try:
            self.assertEqual(2, supervisor.main([]))
        finally:
            if previous is None:
                del os.environ["COMPOSE_UP_TIMEOUT_S"]
            else:
                os.environ["COMPOSE_UP_TIMEOUT_S"] = previous


_original_run = supervisor._run


if __name__ == "__main__":
    unittest.main()
