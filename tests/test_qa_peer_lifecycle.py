"""Loopback plant test peers must stop without leaving socket threads."""
import json
import socket
import time
import unittest

from test_qa_scenarios import ClaimPlantPeer, FakePlantPeer


class SocketPeerLifecycleTests(unittest.TestCase):
    def assert_peer_stops(self, peer_type):
        peer = peer_type()
        host, _, port = peer.address.rpartition(":")
        client = socket.create_connection((host, int(port)), timeout=1)
        try:
            client.sendall(b'{"op":"list_points"}\n')
            response = b""
            while b"\n" not in response:
                chunk = client.recv(65536)
                if not chunk:
                    self.fail("peer closed before returning its response")
                response += chunk
            self.assertEqual(
                json.loads(response.split(b"\n", 1)[0])["result"],
                "points",
            )
            self.assertTrue(peer.conns)
            started = time.monotonic()
            peer.close()
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertFalse(peer.thread.is_alive())
            self.assertFalse(peer.conns)
            self.assertFalse(peer._handler_threads)
        finally:
            client.close()
            if peer.thread.is_alive() or peer._handler_threads:
                peer.close()

    def test_fake_plant_peer_wakes_accept_and_joins_handlers(self):
        self.assert_peer_stops(FakePlantPeer)

    def test_claim_plant_peer_wakes_accept_and_joins_handlers(self):
        self.assert_peer_stops(ClaimPlantPeer)


if __name__ == "__main__":
    unittest.main()
