import os
import json
import tempfile
import unittest

import ngx_bot as ngx


class TestNGXBot(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tmp_dir.name, "ngx_state.json")
        ngx.STATE = self.state_file

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_helpers(self):
        self.assertEqual(ngx.num(" 1,234.56 "), 1234.56)
        self.assertIsNone(ngx.num("invalid"))

        self.assertEqual(ngx.to_date("2026-10-04T12:00:00"), "2026-10-04")
        self.assertEqual(ngx.to_date("04/10/2026"), "2026-10-04")

    def test_market_ctx(self):
        # Construct history for liquid stocks
        hist = {}
        # Stock 1: above 50-day average
        hist["STK1"] = [[f"2026-01-{i:02d}", 10.0 + i*0.1, 2000000.0, 1.0] for i in range(1, 62)]
        # Stock 2: below 50-day average
        hist["STK2"] = [[f"2026-01-{i:02d}", 100.0 - i*0.5, 2000000.0, -1.0] for i in range(1, 62)]

        ctx = ngx.market_ctx(hist)
        self.assertIn("breadth", ctx)
        self.assertIn("rank", ctx)
        self.assertEqual(ctx["breadth"], 0.5)  # 1 out of 2 liquid stocks above 50d avg

    def test_order_fill_simulation(self):
        acct = ngx.new_acct()
        acct["pending"] = [{"side": "BUY", "sym": "ZENITHBANK", "ref": 10.0, "date": "2026-10-04"}]
        quote = {"ZENITHBANK": {"open": 10.0, "close": 10.5}}

        ngx.fill_pending(acct, quote, "test_strat", lambda msg: None)

        self.assertEqual(len(acct["pending"]), 0)
        self.assertEqual(len(acct["pos"]), 1)
        self.assertEqual(acct["pos"][0]["sym"], "ZENITHBANK")
        self.assertLess(acct["cash"], ngx.START)


if __name__ == "__main__":
    unittest.main()
