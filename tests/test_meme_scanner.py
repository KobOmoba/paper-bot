import unittest
import meme_scanner as meme


class TestMemeScanner(unittest.TestCase):

    def test_amm_out(self):
        # Selling into a pool with 10,000 USD total liquidity (5000 USD per side)
        value = 100.0
        liq = 10_000.0
        out = meme.amm_out(value, liq)
        # constant product: value * R / (R + value), where R = 5000
        # 100 * 5000 / 5100 = 98.039...
        self.assertAlmostEqual(out, 100.0 * 5000.0 / 5100.0, places=4)

    def test_amm_out_drained_pool(self):
        # Selling into a drained pool with 0 liquidity should return 0.0
        out = meme.amm_out(100.0, 0.0)
        self.assertEqual(out, 0.0)

    def test_velocity_calc(self):
        now = 10000.0
        # Readings history: [time, 24h volume]
        h = [
            [now - 1600, 10_000.0], # ~26 min ago
            [now - 800, 15_000.0]   # ~13 min ago
        ]
        v24 = 20_000.0
        age_h = 1.0

        res = meme.velocity_calc(h, now, v24, age_h)
        self.assertIsNotNone(res)
        recent, prev = res
        self.assertGreater(recent, 0)
        self.assertGreater(prev, 0)

    def test_tag_of(self):
        self.assertEqual(meme.tag_of(350_000), "ramp-bigliq")
        self.assertEqual(meme.tag_of(10_000), "organic")


if __name__ == "__main__":
    unittest.main()
