"""The slot/port cross-check, which is the part of this service with logic in it.

Weighted heavily towards the ways a declaration and a compose file can disagree,
because that disagreement is the failure this module exists to prevent and it is
invisible at every other layer: the node forwards a slot it was told about, the compose
file publishes what it publishes, and nothing else compares the two.
"""

import json
import unittest

import compose_spec
from compose_spec import ComposeSpecError


def document(services):
    return {"name": "stack", "services": services}


def whoami(published="8080", target=80, protocol="tcp", mode="host"):
    entry = {"mode": mode, "target": target, "protocol": protocol}
    if published is not None:
        entry["published"] = published
    return {"image": "x@sha256:" + "0" * 64, "ports": [entry]}


class ParseConfigJson(unittest.TestCase):
    def test_a_dict_passes_through(self):
        parsed = compose_spec.parse_config_json(document({"a": whoami()}))
        self.assertIn("services", parsed)

    def test_json_text_is_parsed(self):
        parsed = compose_spec.parse_config_json(json.dumps(document({"a": whoami()})))
        self.assertEqual(["a"], compose_spec.service_names(parsed))

    def test_bytes_are_decoded(self):
        raw = json.dumps(document({"a": whoami()})).encode("utf-8")
        self.assertEqual(["a"], compose_spec.service_names(compose_spec.parse_config_json(raw)))

    def test_empty_output_is_refused_naming_the_command(self):
        # The observed shape of "compose is not there" or "the file is empty", and the
        # message has to say which command produced nothing or it is unactionable.
        with self.assertRaises(ComposeSpecError) as caught:
            compose_spec.parse_config_json("   \n  ")
        self.assertIn("docker compose config", str(caught.exception))

    def test_non_json_is_refused(self):
        with self.assertRaises(ComposeSpecError):
            compose_spec.parse_config_json("services:\n  a:\n    image: x\n")

    def test_a_json_list_is_refused(self):
        with self.assertRaises(ComposeSpecError):
            compose_spec.parse_config_json("[1, 2]")

    def test_a_document_with_no_services_is_refused(self):
        with self.assertRaises(ComposeSpecError) as caught:
            compose_spec.parse_config_json({"name": "stack", "services": {}})
        self.assertIn("no services", str(caught.exception))

    def test_invalid_utf8_is_refused(self):
        with self.assertRaises(ComposeSpecError):
            compose_spec.parse_config_json(b"\xff\xfe not json")


class PublishedPorts(unittest.TestCase):
    def test_a_single_published_port(self):
        self.assertEqual({(8080, "tcp")}, compose_spec.published_ports(document({"w": whoami()})))

    def test_published_may_be_an_int(self):
        # Compose has emitted this both ways across versions.
        self.assertEqual(
            {(8080, "tcp")}, compose_spec.published_ports(document({"w": whoami(published=8080)}))
        )

    def test_a_service_with_no_ports_publishes_nothing(self):
        services = {"redis": {"image": "r@sha256:" + "0" * 64}, "w": whoami()}
        self.assertEqual({(8080, "tcp")}, compose_spec.published_ports(document(services)))

    def test_udp_is_kept_distinct_from_tcp(self):
        # A slot declared tcp against a port published udp is a real mismatch, so the
        # protocol has to be part of the identity rather than dropped.
        found = compose_spec.published_ports(document({"w": whoami(protocol="udp")}))
        self.assertEqual({(8080, "udp")}, found)

    def test_a_port_range_is_expanded(self):
        found = compose_spec.published_ports(document({"w": whoami(published="8080-8082")}))
        self.assertEqual({(8080, "tcp"), (8081, "tcp"), (8082, "tcp")}, found)

    def test_a_backwards_range_is_refused(self):
        with self.assertRaises(ComposeSpecError):
            compose_spec.published_ports(document({"w": whoami(published="9000-8000")}))

    def test_an_enormous_range_is_refused_rather_than_expanded(self):
        # `published: "1-65535"` would otherwise build a 65k-element set from a line in
        # someone's compose file.
        with self.assertRaises(ComposeSpecError) as caught:
            compose_spec.published_ports(document({"w": whoami(published="1-65535")}))
        self.assertIn("1024", str(caught.exception))

    def test_an_empty_published_is_refused_with_advice(self):
        # compose's "pick a free host port". An API slot is fixed at pack time, so such
        # a port can never be reachable -- and the message has to say what to do.
        with self.assertRaises(ComposeSpecError) as caught:
            compose_spec.published_ports(document({"w": whoami(published="")}))
        message = str(caught.exception)
        self.assertIn("no fixed host port", message)
        self.assertIn("published", message)

    def test_a_missing_published_is_refused(self):
        with self.assertRaises(ComposeSpecError):
            compose_spec.published_ports(document({"w": whoami(published=None)}))

    def test_published_true_is_not_port_one(self):
        # bool is a subclass of int in Python, so without an explicit guard
        # `published: true` becomes port 1.
        with self.assertRaises(ComposeSpecError):
            compose_spec.published_ports(document({"w": whoami(published=True)}))

    def test_port_zero_is_refused(self):
        with self.assertRaises(ComposeSpecError):
            compose_spec.published_ports(document({"w": whoami(published="0")}))

    def test_a_port_above_65535_is_refused(self):
        with self.assertRaises(ComposeSpecError):
            compose_spec.published_ports(document({"w": whoami(published="70000")}))

    def test_a_short_form_entry_is_refused_naming_the_reason(self):
        # `docker compose config --format json` always normalises to long form, so a
        # string here means this document did not come from it.
        services = {"w": {"image": "x", "ports": ["8080:80"]}}
        with self.assertRaises(ComposeSpecError) as caught:
            compose_spec.published_ports(document(services))
        self.assertIn("long form", str(caught.exception))

    def test_an_unknown_protocol_is_refused(self):
        with self.assertRaises(ComposeSpecError):
            compose_spec.published_ports(document({"w": whoami(protocol="sctp")}))

    def test_expose_is_not_read_as_published(self):
        # `expose:` is reachable only inside the compose network. A slot pointing at one
        # would forward to nothing, so it must not count as published.
        services = {"redis": {"image": "r", "expose": ["6379"]}}
        self.assertEqual(set(), compose_spec.published_ports(document(services)))

    def test_several_services_publishing_several_ports(self):
        services = {
            "api": {"image": "a", "ports": [
                {"target": 80, "published": "8080", "protocol": "tcp"},
                {"target": 443, "published": "8443", "protocol": "tcp"},
            ]},
            "metrics": whoami(published="9100", target=9100),
        }
        self.assertEqual(
            {(8080, "tcp"), (8443, "tcp"), (9100, "tcp")},
            compose_spec.published_ports(document(services)),
        )

    def test_a_non_mapping_service_is_refused(self):
        with self.assertRaises(ComposeSpecError):
            compose_spec.published_ports(document({"w": "image: x"}))

    def test_a_non_list_ports_is_refused(self):
        with self.assertRaises(ComposeSpecError):
            compose_spec.published_ports(document({"w": {"image": "x", "ports": "8080:80"}}))


class SlotPorts(unittest.TestCase):
    def test_a_tcp_slot(self):
        self.assertEqual({(8080, "tcp")}, compose_spec.slot_ports([{"port": 8080, "transport": "tcp"}]))

    def test_transport_defaults_to_tcp(self):
        # PACKING.md: "Defaults to ["tcp"]".
        self.assertEqual({(8080, "tcp")}, compose_spec.slot_ports([{"port": 8080}]))

    def test_a_transport_list_yields_one_entry_per_recognised_transport(self):
        found = compose_spec.slot_ports([{"port": 53, "transport": ["tcp", "udp"]}])
        self.assertEqual({(53, "tcp"), (53, "udp")}, found)

    def test_an_application_protocol_in_the_transport_field_is_skipped_not_refused(self):
        # PACKING.md's own example is `"transport": ["tcp", "http"]`. Refusing `http`
        # would make this validator stricter than the packer it validates against; the
        # port must still be checked, under tcp.
        found = compose_spec.slot_ports([{"port": 8080, "transport": ["tcp", "http"]}])
        self.assertEqual({(8080, "tcp")}, found)

    def test_a_slot_whose_transport_names_nothing_recognisable_still_checks_tcp(self):
        # Dropping it would silently stop checking the slot, which is worse than
        # checking it under the packer's default.
        found = compose_spec.slot_ports([{"port": 8080, "transport": ["http"]}])
        self.assertEqual({(8080, "tcp")}, found)

    def test_a_slot_with_no_port_is_refused(self):
        with self.assertRaises(ComposeSpecError) as caught:
            compose_spec.slot_ports([{"transport": "tcp"}])
        self.assertIn("no port", str(caught.exception))

    def test_a_non_object_slot_is_refused(self):
        with self.assertRaises(ComposeSpecError):
            compose_spec.slot_ports([8080])

    def test_a_string_port_is_read(self):
        self.assertEqual({(8080, "tcp")}, compose_spec.slot_ports([{"port": "8080"}]))

    def test_a_nonsense_port_is_refused(self):
        with self.assertRaises(ComposeSpecError):
            compose_spec.slot_ports([{"port": "eighty"}])

    def test_no_slots_is_an_empty_set_not_an_error(self):
        self.assertEqual(set(), compose_spec.slot_ports([]))
        self.assertEqual(set(), compose_spec.slot_ports(None))


class CrossCheck(unittest.TestCase):
    def test_a_matching_declaration_passes_and_returns_what_is_published(self):
        published = compose_spec.cross_check(document({"w": whoami()}), [{"port": 8080}])
        self.assertEqual({(8080, "tcp")}, published)

    def test_a_slot_the_stack_does_not_publish_is_refused(self):
        with self.assertRaises(ComposeSpecError) as caught:
            compose_spec.cross_check(document({"w": whoami()}), [{"port": 8081}])
        message = str(caught.exception)
        # Both lists in the message, because "8081 is wrong" without "8080 is what you
        # have" is a message that sends someone to read two files.
        self.assertIn("8081/tcp", message)
        self.assertIn("8080/tcp", message)

    def test_a_published_port_with_no_slot_is_allowed(self):
        # The asymmetry that makes this usable on someone else's compose file: a metrics
        # port or an admin port published for a sibling's benefit is ordinary.
        published = compose_spec.cross_check(
            document({"w": whoami(), "m": whoami(published="9100", target=9100)}),
            [{"port": 8080}],
        )
        self.assertEqual({(8080, "tcp"), (9100, "tcp")}, published)

    def test_the_health_port_is_ignored_because_this_service_serves_it(self):
        # The health slot is in service.json and no container publishes it. Without the
        # exemption every correct declaration would fail.
        published = compose_spec.cross_check(
            document({"w": whoami()}), [{"port": 8080}, {"port": 9000}], ignore_ports=[9000]
        )
        self.assertEqual({(8080, "tcp")}, published)

    def test_the_health_port_is_not_ignored_when_not_passed(self):
        with self.assertRaises(ComposeSpecError):
            compose_spec.cross_check(document({"w": whoami()}), [{"port": 8080}, {"port": 9000}])

    def test_a_protocol_mismatch_is_caught(self):
        # Published udp, declared tcp. The node writes a tcp forward to a port where
        # only udp is bound, which fails at the first connection and nowhere earlier.
        with self.assertRaises(ComposeSpecError) as caught:
            compose_spec.cross_check(
                document({"w": whoami(protocol="udp")}), [{"port": 8080, "transport": "tcp"}]
            )
        self.assertIn("8080/tcp", str(caught.exception))

    def test_a_declaration_with_no_slots_passes_trivially(self):
        self.assertEqual({(8080, "tcp")}, compose_spec.cross_check(document({"w": whoami()}), []))

    def test_the_message_explains_why_this_is_refused_at_startup(self):
        # The error is the whole user interface of this check, so its content is part of
        # the contract rather than incidental.
        with self.assertRaises(ComposeSpecError) as caught:
            compose_spec.cross_check(document({"w": whoami()}), [{"port": 9999}])
        message = str(caught.exception)
        self.assertIn("looks healthy and refuses every connection", message)
        self.assertIn("service.json", message)


class ServiceNames(unittest.TestCase):
    def test_names_are_sorted(self):
        names = compose_spec.service_names(document({"w": whoami(), "a": whoami(published="81")}))
        self.assertEqual(["a", "w"], names)

    def test_a_document_with_no_services_yields_nothing(self):
        self.assertEqual([], compose_spec.service_names({"name": "x"}))


class LoadServiceJson(unittest.TestCase):
    def test_a_json_string_is_read(self):
        parsed = compose_spec.load_service_json('{"api": [{"port": 8080}]}')
        self.assertEqual([{"port": 8080}], parsed["api"])

    def test_malformed_json_is_refused_naming_the_file(self):
        with self.assertRaises(ComposeSpecError) as caught:
            compose_spec.load_service_json("{not json")
        self.assertIn("service.json", str(caught.exception))

    def test_a_non_object_is_refused(self):
        with self.assertRaises(ComposeSpecError):
            compose_spec.load_service_json("[]")

    def test_a_non_list_api_is_refused(self):
        with self.assertRaises(ComposeSpecError):
            compose_spec.load_service_json('{"api": 8080}')

    def test_a_declaration_with_no_api_is_fine(self):
        self.assertEqual({}, dict(compose_spec.load_service_json("{}")))


class TheRealFilesAgree(unittest.TestCase):
    """The repo's own stack and declaration, checked against each other.

    This is the test that would have caught the mistake this module is about, in this
    repository, and it runs with no Docker: the committed `stack/docker-compose.yml` and
    the committed `.service/service.json` have to agree.
    """

    def test_the_example_stack_publishes_every_declared_slot(self):
        import os

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, ".service", "service.json"), encoding="utf-8") as handle:
            declaration = json.load(handle)

        # The compose file is read through the build-time reader rather than compose
        # itself, because this test must not need Docker. `tests/test_image.sh` does the
        # same check through `docker compose config` where a daemon exists.
        import sys

        sys.path.insert(0, os.path.join(root, ".service"))
        import preflight

        with open(os.path.join(root, "stack", "docker-compose.yml"), encoding="utf-8") as handle:
            published, notes = preflight.published_ports(handle.read())

        self.assertEqual([], notes, f"the example stack has constructs the reader skipped: {notes}")
        declared = preflight.slot_ports(declaration)
        health = {preflight.health_port(declaration)}
        missing = {
            (port, protocol)
            for port, protocol in declared
            if port not in health and (port, protocol) not in published
        }
        self.assertEqual(set(), missing, f"declared but not published: {missing}")


if __name__ == "__main__":
    unittest.main()
