import unittest
import us_live as us


class TestUSLive(unittest.TestCase):

    def test_signal_calculation(self):
        # Construct history for tickers with LOOK (126) + 1 sessions
        hist = {}
        for idx, ticker in enumerate(["AAPL", "MSFT", "SPY"]):
            base_px = 100.0
            rows = []
            for i in range(130):
                px = base_px + (i * (idx + 1) * 0.1)
                vol = 1_000_000.0
                rows.append((f"2026-01-{i+1}", px, vol))
            hist[ticker] = rows

        # Calculate signal
        us.LOOK = 126
        us.N = 2
        us.MIN_DV = 100_000
        pend, score = us.signal(hist)

        self.assertIn("MSFT", pend)
        self.assertIn("AAPL", pend)
        self.assertEqual(len(pend), 2)
        self.assertAlmostEqual(pend["MSFT"], 0.5)

    def test_equity_calculation(self):
        acct = {
            "cash": 1000.0,
            "pos": {"AAPL": 10, "MSFT": 5},
            "last_px": {"AAPL": 150.0, "MSFT": 300.0}
        }
        px = {"AAPL": 160.0}  # MSFT relies on last_px
        # Equity = 1000 + (10 * 160) + (5 * 300) = 1000 + 1600 + 1500 = 4100
        eq = us.equity(acct, px)
        self.assertEqual(eq, 4100.0)


if __name__ == "__main__":
    unittest.main()
