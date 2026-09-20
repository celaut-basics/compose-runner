"""The health slot: compose's output in, an HTTP status and a body out.

The whole point of this endpoint is to distinguish "the VM is up" from "the stack is
up", so these tests are mostly about which states become 200 and which become 503 --
the distinction a caller actually acts on.

Tested against real captures of what `docker compose ps --format json` writes (see
`tests/fixtures/`), not against invented JSON, because the key names are capitalised in
a way that is easy to get wrong from memory and a parser tested against its own
assumptions proves nothing.
"""

import json
import os
import unittest
import urllib.error
import urllib.request

import health

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return handle.read()


class ParsePs(unittest.TestCase):
    def test_a_real_two_container_capture(self):
        records = health.parse_ps(fixture("compose-ps-running.json"))
        self.assertEqual(2, len(records))
        # The `Service` field rather than `Name`: a container's name carries whatever
        # `-p` the capture happened to be taken under, while `Service` identifies which
        # compose service it is -- which is the thing that should be stable.
        self.assertEqual({"redis", "whoami"}, {r["Service"] for r in records})
        self.assertTrue(all(r["State"] == "running" for r in records))

    def test_newline_delimited_objects_are_read(self):
        # What compose v2 wrote before it emitted an array. Handled because the compose
        # version is pinned today and this should not break on the next bump.
        text = '{"Name": "a", "State": "running"}\n{"Name": "b", "State": "running"}\n'
        self.assertEqual(2, len(health.parse_ps(text)))

    def test_a_single_object_is_read_as_one_record(self):
        self.assertEqual(1, len(health.parse_ps('{"Name": "a", "State": "running"}')))

    def test_an_array_of_objects_is_read(self):
        self.assertEqual(2, len(health.parse_ps('[{"Name":"a"},{"Name":"b"}]')))

    def test_bytes_are_decoded(self):
        self.assertEqual(1, len(health.parse_ps(b'[{"Name": "a"}]')))

    def test_an_already_parsed_list_passes_through(self):
        self.assertEqual(1, len(health.parse_ps([{"Name": "a"}])))

    def test_empty_output_is_no_records_not_an_error(self):
        # A health endpoint must degrade, not raise: "I could not read compose" is an
        # answer to report.
        self.assertEqual([], health.parse_ps(""))
        self.assertEqual([], health.parse_ps("   \n"))

    def test_unparseable_output_is_no_records_not_an_error(self):
        self.assertEqual([], health.parse_ps("Cannot connect to the Docker daemon"))

    def test_non_dict_elements_are_dropped(self):
        self.assertEqual([{"Name": "a"}], health.parse_ps('[{"Name":"a"}, 7, null]'))

    def test_a_json_scalar_is_no_records(self):
        self.assertEqual([], health.parse_ps("42"))


class Summarise(unittest.TestCase):
    def test_every_container_running_is_200_ok(self):
        code, document = health.summarise(health.parse_ps(fixture("compose-ps-running.json")), expected=2)
        self.assertEqual(200, code)
        self.assertEqual("ok", document["status"])
        self.assertEqual(2, document["running"])
        self.assertEqual(2, document["total"])

    def test_no_containers_is_503_starting(self):
        # The window this endpoint exists for: the VM answers, the stack has not started.
        code, document = health.summarise([], expected=2)
        self.assertEqual(503, code)
        self.assertEqual("starting", document["status"])
        self.assertIn("no containers", document["detail"])

    def test_an_exited_container_is_503_degraded(self):
        code, document = health.summarise(health.parse_ps(fixture("compose-ps-exited.json")), expected=2)
        self.assertEqual(503, code)
        self.assertEqual("degraded", document["status"])
        self.assertEqual(1, document["running"])

    def test_the_exit_code_of_a_failed_container_is_reported(self):
        _, document = health.summarise(health.parse_ps(fixture("compose-ps-exited.json")))
        failed = [c for c in document["containers"] if c["state"] != "running"]
        self.assertEqual(1, len(failed))
        self.assertEqual(1, failed[0]["exit_code"])

    def test_an_unhealthy_container_is_503_even_though_it_is_running(self):
        # The entire point of an operator writing `healthcheck:` in their compose file.
        # State is "running" and Health is "unhealthy"; reporting 200 would ignore the
        # check they asked for.
        records = health.parse_ps(fixture("compose-ps-unhealthy.json"))
        code, document = health.summarise(records, expected=1)
        self.assertEqual(503, code)
        self.assertEqual("degraded", document["status"])

    def test_a_healthy_healthcheck_is_reported_and_still_200(self):
        records = [{"Name": "a", "Service": "a", "State": "running", "Health": "healthy"}]
        code, document = health.summarise(records, expected=1)
        self.assertEqual(200, code)
        self.assertEqual("healthy", document["containers"][0]["health"])

    def test_fewer_containers_than_the_compose_file_declares_is_starting(self):
        # compose's `ps` only lists containers that exist, so "all running" over a short
        # list is a trap without the expected count.
        records = [{"Name": "a", "Service": "a", "State": "running"}]
        code, document = health.summarise(records, expected=3)
        self.assertEqual(503, code)
        self.assertEqual("starting", document["status"])
        self.assertIn("1 of 3", document["detail"])

    def test_expected_is_omitted_from_the_body_when_not_given(self):
        _, document = health.summarise([{"Name": "a", "State": "running"}])
        self.assertNotIn("expected", document)

    def test_containers_are_sorted_so_the_body_is_stable(self):
        records = [
            {"Name": "z", "State": "running"},
            {"Name": "a", "State": "running"},
        ]
        _, document = health.summarise(records)
        self.assertEqual(["a", "z"], [c["name"] for c in document["containers"]])

    def test_an_unreadable_record_degrades_rather_than_raising(self):
        # A record with none of the expected keys must not take the health check down.
        code, document = health.summarise([{}])
        self.assertEqual(503, code)
        self.assertEqual("?", document["containers"][0]["name"])

    def test_lowercase_keys_are_read_too(self):
        records = [{"name": "a", "service": "a", "state": "running"}]
        code, _ = health.summarise(records, expected=1)
        self.assertEqual(200, code)

    def test_state_is_compared_case_insensitively(self):
        code, _ = health.summarise([{"Name": "a", "State": "Running"}], expected=1)
        self.assertEqual(200, code)


class Provider(unittest.TestCase):
    """The provider, with the subprocess replaced -- no Docker needed."""

    def test_a_successful_ps_is_summarised(self):
        provider = health.compose_ps_provider(
            ["docker", "compose", "ps"],
            expected=2,
            runner=lambda argv: (0, fixture("compose-ps-running.json"), ""),
        )
        code, document = provider()
        self.assertEqual(200, code)
        self.assertEqual("ok", document["status"])

    def test_a_failed_ps_is_503_with_the_stderr_as_the_detail(self):
        # dockerd down, socket gone, project missing: all mean "cannot confirm the stack
        # is up", and the stderr is what tells an operator which.
        provider = health.compose_ps_provider(
            ["docker", "compose", "ps"],
            runner=lambda argv: (1, "", "Cannot connect to the Docker daemon at unix:///run/docker.sock"),
        )
        code, document = provider()
        self.assertEqual(503, code)
        self.assertEqual("error", document["status"])
        self.assertIn("Cannot connect", document["detail"])
        self.assertEqual(1, document["exit_code"])

    def test_a_long_stderr_is_truncated(self):
        provider = health.compose_ps_provider(
            ["docker"], runner=lambda argv: (1, "", "x" * 5000)
        )
        _, document = provider()
        self.assertLessEqual(len(document["detail"]), 500)

    def test_the_argv_is_passed_through_as_a_list(self):
        seen = {}

        def runner(argv):
            seen["argv"] = argv
            return 0, "[]", ""

        provider = health.compose_ps_provider(["docker", "compose", "ps"], runner=runner)
        provider()
        self.assertEqual(["docker", "compose", "ps"], seen["argv"])
        self.assertIsInstance(seen["argv"], list)


class Server(unittest.TestCase):
    """The HTTP contract, against a real socket on a real port."""

    def setUp(self):
        self.status = (200, {"status": "ok"})
        self.server = health.serve(0, lambda: self.status)
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def test_health_answers_200_when_the_stack_is_up(self):
        code, document = self.get("/health")
        self.assertEqual(200, code)
        self.assertEqual("ok", document["status"])

    def test_health_answers_503_when_the_stack_is_not(self):
        self.status = (503, {"status": "starting"})
        code, document = self.get("/health")
        self.assertEqual(503, code)
        self.assertEqual("starting", document["status"])

    def test_the_root_path_also_answers(self):
        # A bare `curl http://instance:9000/` is what someone tries first.
        self.assertEqual(200, self.get("/")[0])

    def test_a_trailing_slash_is_accepted(self):
        self.assertEqual(200, self.get("/health/")[0])

    def test_a_query_string_is_ignored(self):
        # Probes add cache-busting parameters.
        self.assertEqual(200, self.get("/health?t=1")[0])

    def test_an_unknown_path_is_404_with_the_paths_it_serves(self):
        code, document = self.get("/nope")
        self.assertEqual(404, code)
        self.assertIn("/health", document["paths"])

    def test_a_provider_that_raises_becomes_503_and_not_a_500(self):
        # A health endpoint that 500s on its own bug tells a caller nothing about the
        # stack. The exception type and message are reported instead.
        def broken():
            raise RuntimeError("the daemon vanished")

        self.server.shutdown()
        self.server.server_close()
        self.server = health.serve(0, broken)
        self.port = self.server.server_address[1]
        code, document = self.get("/health")
        self.assertEqual(503, code)
        self.assertIn("the daemon vanished", document["detail"])
        self.assertIn("RuntimeError", document["detail"])

    def test_the_body_is_json_with_a_content_length(self):
        url = f"http://127.0.0.1:{self.port}/health"
        with urllib.request.urlopen(url, timeout=5) as response:
            self.assertEqual("application/json", response.headers["Content-Type"])
            self.assertTrue(int(response.headers["Content-Length"]) > 0)


if __name__ == "__main__":
    unittest.main()
