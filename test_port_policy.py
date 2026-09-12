import unittest

import port_policy


REGISTRY = {
    "reservations": [
        {"name": "Victoria backend", "port": 8765, "bind": "loopback"},
        {"name": "ReActor Lab", "port": 8766, "bind": "loopback"},
    ]
}


class PortPolicyTests(unittest.TestCase):
    def test_collision_moves_to_next_unreserved_port(self):
        result = port_policy.validate_port_request(
            service_name="new-api",
            preferred_port=8765,
            range_start=8765,
            range_end=8768,
            bind="loopback",
            listeners=[{"port": 8765, "pid": 1, "process": "python"}],
            registry=REGISTRY,
        )
        self.assertEqual(result["assigned_port"], 8767)
        self.assertTrue(result["port_changed"])

    def test_matching_reservation_can_be_used(self):
        result = port_policy.validate_port_request(
            service_name="victoria",
            preferred_port=8765,
            range_start=8765,
            range_end=8767,
            bind="loopback",
            reservation_name="Victoria backend",
            listeners=[],
            registry=REGISTRY,
        )
        self.assertEqual(result["assigned_port"], 8765)

    def test_unknown_service_cannot_take_fixed_reservation(self):
        chosen = port_policy.choose_port(
            8765,
            8765,
            8766,
            reservations={8765: "Victoria backend", 8766: "ReActor Lab"},
            occupied_ports=set(),
        )
        self.assertIsNone(chosen)

    def test_invalid_large_range_fails_closed(self):
        with self.assertRaises(port_policy.PortPolicyError):
            port_policy.choose_port(9000, 9000, 11001, reservations={}, occupied_ports=set())


if __name__ == "__main__":
    unittest.main()
