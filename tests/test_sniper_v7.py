import unittest
import sniper_v7 as v7


class TestSniperV7(unittest.TestCase):

    def test_curve_maths(self):
        vs, vt = 30.0, 1073000191.0
        sol_in = 0.05
        toks, avg_px = v7.curve_buy(vs, vt, sol_in)
        self.assertGreater(toks, 0)
        self.assertGreater(avg_px, 0)

        sol_out = v7.curve_sell(vs, vt, toks)
        # Due to fee and Jito tip, sell proceeds should be slightly lower than input
        self.assertLess(sol_out, sol_in)

    def test_act_state_machine_tier_targets(self):
        # Setup position
        pos = {
            "status": "OPEN",
            "tokens0": 1000.0,
            "tokens": 1000.0,
            "cost": 0.05,
            "proceeds": 0.0,
            "ath": 0.001,
            "entry_t": 1000,
            "prev_liq": 30.0
        }

        # Mock sell_fn returning multiple of cost
        # Scenario 1: price doubled -> hit 2x Tier 1
        sell_fn_2x = lambda t: 0.10 * (t / 1000.0) # capital multiple = 0.10 / 0.05 = 2.0x
        events = v7.act(pos, mid=0.002, liq=30.0, sell_fn=sell_fn_2x, now=1100)

        self.assertEqual(pos["status"], "TIER_1_HIT")
        self.assertEqual(pos["tokens"], 400.0) # sold 60%
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0], ("tier", 2))

    def test_act_state_machine_rug_collapse(self):
        pos = {
            "status": "OPEN",
            "tokens0": 1000.0,
            "tokens": 1000.0,
            "cost": 0.05,
            "proceeds": 0.0,
            "ath": 0.001,
            "entry_t": 1000,
            "prev_liq": 30.0
        }

        sell_fn = lambda t: 0.01
        # Liquidity drops from 30.0 to 10.0 (>60% drop)
        events = v7.act(pos, mid=0.0005, liq=10.0, sell_fn=sell_fn, now=1010)

        self.assertEqual(pos["status"], "CLOSED")
        self.assertEqual(pos["reason"], "rug-liquidity-collapse")
        self.assertEqual(pos["tokens"], 0.0)


if __name__ == "__main__":
    unittest.main()
