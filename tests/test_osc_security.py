from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.eeg.metrics import EffortEstimator
from backend.eeg.sources import OSCSource


class OSCSourceSecurityTests(unittest.TestCase):
    def test_external_osc_is_disabled_until_a_sender_is_explicitly_paired(self) -> None:
        with patch.dict(os.environ, {"MUSE_OSC_HOST": ""}):
            source = OSCSource(EffortEstimator(), port=5001)

        self.assertEqual(source.bind_host, "127.0.0.1")
        self.assertEqual(source.allowed_host, "127.0.0.1")
        self.assertTrue(source._sender_allowed(("127.0.0.1", 49000)))
        self.assertFalse(source._sender_allowed(("192.168.50.20", 49000)))

    def test_only_the_exact_paired_ipv4_sender_is_accepted(self) -> None:
        source = OSCSource(
            EffortEstimator(), port=5001, allowed_host="192.168.50.20"
        )

        self.assertEqual(source.bind_host, "0.0.0.0")
        self.assertTrue(source._sender_allowed(("192.168.50.20", 49000)))
        self.assertFalse(source._sender_allowed(("192.168.50.21", 49000)))
        self.assertFalse(source._sender_allowed(("not-an-address", 49000)))
        self.assertFalse(source._sender_allowed(None))

    def test_foreign_osc_clench_cannot_create_flags(self) -> None:
        estimator = EffortEstimator()
        source = OSCSource(
            estimator, port=5001, allowed_host="192.168.50.20"
        )
        guarded_clench = source._trusted(source._on_clench)

        guarded_clench(
            ("192.168.50.99", 49000),
            "/muse/elements/jaw_clench",
            1.0,
        )

        self.assertEqual(estimator.drain_flags(), [])
        self.assertEqual(source.rejected_packets, 1)

        guarded_clench(
            ("192.168.50.20", 49000),
            "/muse/elements/jaw_clench",
            1.0,
        )
        self.assertEqual(estimator.drain_flags()[0]["source"], "jaw_clench")

    def test_sensor_flags_do_not_call_the_privileged_promote_api(self) -> None:
        web_dir = Path(__file__).resolve().parent.parent / "web"
        app_js = (web_dir / "app.js").read_text(encoding="utf-8")
        index_html = (web_dir / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("promote(msg.flag.zone)", app_js)
        self.assertIn("Jaw clench · flag", index_html)
        self.assertNotIn("Jaw clench · confirm", index_html)

    def test_sender_pairing_rejects_ambiguous_addresses(self) -> None:
        for value in ("phone.local", "0.0.0.0", "224.0.0.1", "::1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                OSCSource(EffortEstimator(), port=5001, allowed_host=value)


if __name__ == "__main__":
    unittest.main()
