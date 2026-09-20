"""The build-time reader, whose contract is "be right or admit you skipped it".

`.service/preflight.py` parses a compose file with a deliberately tiny YAML subset,
because PyYAML is not in the image and `docker compose config` cannot be used in a build
step (it would either substitute the build's environment for `${VAR}` or refuse).

So the tests here are in two halves, and the second is the important one:

1. it reads the constructs it claims to read -- long form, short form, ranges, inline;
2. everything else produces a **note**, and a note downgrades a failure to a warning.

That second property is what keeps a partial parser from being the thing that refuses a
correct build.
"""

import json
import os
import unittest

import preflight

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class LongForm(unittest.TestCase):
    def test_the_repos_own_stack_is_read_with_no_notes(self):
        # If this ever produces a note, the example stack has grown a construct the
        # build-time check silently stops covering.
        with open(os.path.join(ROOT, "stack", "docker-compose.yml"), encoding="utf-8") as handle:
            published, notes = preflight.published_ports(handle.read())
        self.assertEqual([], notes)
        self.assertEqual({(8080, "tcp")}, published)

    def test_a_long_form_entry(self):
        text = """
services:
  w:
    image: x
    ports:
      - target: 80
        published: "8080"
        protocol: tcp
"""
        self.assertEqual(({(8080, "tcp")}, []), preflight.published_ports(text))

    def test_an_unquoted_published_value(self):
        text = """
services:
  w:
    ports:
      - target: 80
        published: 8080
"""
        self.assertEqual({(8080, "tcp")}, preflight.published_ports(text)[0])

    def test_protocol_defaults_to_tcp_when_the_entry_names_none(self):
        text = """
services:
  w:
    ports:
      - target: 80
        published: "8080"
"""
        self.assertEqual({(8080, "tcp")}, preflight.published_ports(text)[0])

    def test_udp_is_read(self):
        text = """
services:
  w:
    ports:
      - target: 53
        published: "5353"
        protocol: udp
"""
        self.assertEqual({(5353, "udp")}, preflight.published_ports(text)[0])

    def test_two_long_form_entries_in_one_service(self):
        text = """
services:
  w:
    ports:
      - target: 80
        published: "8080"
        protocol: tcp
      - target: 443
        published: "8443"
        protocol: tcp
"""
        self.assertEqual({(8080, "tcp"), (8443, "tcp")}, preflight.published_ports(text)[0])

    def test_the_protocol_of_one_entry_does_not_leak_into_the_next(self):
        # The pending-entry flush exists for this: without it, a udp entry followed by a
        # tcp one would report both as udp.
        text = """
services:
  w:
    ports:
      - target: 53
        published: "5353"
        protocol: udp
      - target: 80
        published: "8080"
"""
        self.assertEqual({(5353, "udp"), (8080, "tcp")}, preflight.published_ports(text)[0])


class ShortForm(unittest.TestCase):
    def test_host_and_container(self):
        text = 'services:\n  w:\n    ports:\n      - "8080:80"\n'
        self.assertEqual({(8080, "tcp")}, preflight.published_ports(text)[0])

    def test_unquoted(self):
        text = "services:\n  w:\n    ports:\n      - 8080:80\n"
        self.assertEqual({(8080, "tcp")}, preflight.published_ports(text)[0])

    def test_with_a_bind_address(self):
        text = 'services:\n  w:\n    ports:\n      - "127.0.0.1:8080:80"\n'
        self.assertEqual({(8080, "tcp")}, preflight.published_ports(text)[0])

    def test_with_a_protocol_suffix(self):
        text = 'services:\n  w:\n    ports:\n      - "5353:53/udp"\n'
        self.assertEqual({(5353, "udp")}, preflight.published_ports(text)[0])

    def test_a_range(self):
        text = 'services:\n  w:\n    ports:\n      - "8080-8081:80-81"\n'
        self.assertEqual({(8080, "tcp"), (8081, "tcp")}, preflight.published_ports(text)[0])

    def test_a_container_only_port_is_noted_not_read(self):
        # `- "80"` means "compose picks a free host port", which no API slot can name.
        # A note, because the startup check is the one that gets to refuse it.
        published, notes = preflight.published_ports('services:\n  w:\n    ports:\n      - "80"\n')
        self.assertEqual(set(), published)
        self.assertEqual(1, len(notes))

    def test_an_inline_flow_sequence(self):
        text = 'services:\n  w:\n    ports: ["8080:80", "8443:443"]\n'
        self.assertEqual({(8080, "tcp"), (8443, "tcp")}, preflight.published_ports(text)[0])

    def test_an_empty_inline_sequence(self):
        self.assertEqual(set(), preflight.published_ports('services:\n  w:\n    ports: []\n')[0])


class WhatItSkips(unittest.TestCase):
    def test_an_interpolated_port_is_noted_rather_than_guessed(self):
        # This is exactly why `docker compose config` cannot be used at build time, and
        # the reader must not invent a value for it.
        published, notes = preflight.published_ports(
            'services:\n  w:\n    ports:\n      - "${HOST_PORT}:80"\n'
        )
        self.assertEqual(set(), published)
        self.assertTrue(notes)

    def test_a_note_is_what_downgrades_a_failure_to_a_warning(self):
        # The property the whole design rests on: a partial parser must not fail a build
        # it merely failed to read.
        _, notes = preflight.published_ports('services:\n  w:\n    ports:\n      - "${P}:80"\n')
        self.assertTrue(notes)

    def test_expose_is_not_read_as_published(self):
        text = "services:\n  redis:\n    image: r\n    expose:\n      - \"6379\"\n"
        self.assertEqual(set(), preflight.published_ports(text)[0])

    def test_the_ports_block_ends_at_the_next_key(self):
        # Without the indentation check, `environment:` values would be parsed as ports.
        text = """
services:
  w:
    ports:
      - "8080:80"
    environment:
      - "PORT=9999"
    restart: unless-stopped
"""
        published, notes = preflight.published_ports(text)
        self.assertEqual({(8080, "tcp")}, published)
        self.assertEqual([], notes)

    def test_a_comment_after_a_port_is_ignored(self):
        text = 'services:\n  w:\n    ports:\n      - "8080:80"  # the API slot\n'
        self.assertEqual({(8080, "tcp")}, preflight.published_ports(text)[0])

    def test_a_compose_file_with_no_ports_at_all(self):
        text = "services:\n  redis:\n    image: r\n"
        self.assertEqual((set(), []), preflight.published_ports(text))


class SlotPorts(unittest.TestCase):
    def test_slots_are_read_from_a_declaration(self):
        declaration = {"api": [{"port": 8080, "transport": "tcp"}, {"port": 9000}]}
        self.assertEqual({(8080, "tcp"), (9000, "tcp")}, preflight.slot_ports(declaration))

    def test_a_transport_list(self):
        self.assertEqual(
            {(53, "tcp"), (53, "udp")},
            preflight.slot_ports({"api": [{"port": 53, "transport": ["tcp", "udp"]}]}),
        )

    def test_an_application_protocol_in_transport_falls_back_to_tcp(self):
        self.assertEqual(
            {(8080, "tcp")}, preflight.slot_ports({"api": [{"port": 8080, "transport": ["http"]}]})
        )

    def test_a_declaration_with_no_api(self):
        self.assertEqual(set(), preflight.slot_ports({}))

    def test_an_unreadable_slot_is_skipped_rather_than_raising(self):
        # The build-time reader's job is to warn, so a malformed slot is the startup
        # check's problem, not a reason to abort a build with a traceback.
        self.assertEqual(set(), preflight.slot_ports({"api": [{"port": "eighty"}, 7]}))


class Main(unittest.TestCase):
    """The exit statuses, which are what the Dockerfile's `RUN` reacts to."""

    def setUp(self):
        import tempfile

        self.directory = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.directory, ignore_errors=True)

    def write(self, name, text):
        path = os.path.join(self.directory, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_the_repos_real_files_pass(self):
        code = preflight.main([
            os.path.join(ROOT, "stack", "docker-compose.yml"),
            os.path.join(ROOT, ".service", "service.json"),
        ])
        self.assertEqual(0, code)

    def test_a_slot_the_stack_does_not_publish_fails_the_build(self):
        compose = self.write("c.yml", 'services:\n  w:\n    ports:\n      - "8080:80"\n')
        service = self.write("s.json", json.dumps({"api": [{"port": 9999}]}))
        self.assertEqual(1, preflight.main([compose, service]))

    def test_the_health_port_is_exempt(self):
        # It is a slot no container publishes, so without the exemption every correct
        # declaration would fail the build.
        compose = self.write("c.yml", 'services:\n  w:\n    ports:\n      - "8080:80"\n')
        service = self.write("s.json", json.dumps({"api": [{"port": 8080}, {"port": 9000}]}))
        self.assertEqual(0, preflight.main([compose, service]))

    def test_a_mismatch_the_reader_could_not_fully_parse_only_warns(self):
        # The safety valve: the reader admitted it skipped something, so the port it did
        # not find may well be published by a construct it cannot read.
        compose = self.write("c.yml", 'services:\n  w:\n    ports:\n      - "${PORT}:80"\n')
        service = self.write("s.json", json.dumps({"api": [{"port": 8080}]}))
        self.assertEqual(0, preflight.main([compose, service]))

    def test_wrong_arguments_exit_two(self):
        self.assertEqual(2, preflight.main(["only-one"]))


if __name__ == "__main__":
    unittest.main()
