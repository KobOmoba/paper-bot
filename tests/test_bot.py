import os
import json
import tempfile
import unittest
from unittest.mock import patch

import bot


class TestPolymarketBot(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tmp_dir.name, "state.json")
        bot.STATE = self.state_file

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_load_and_save_state(self):
        # Initial load when file doesn't exist
        s = bot.load()
        self.assertEqual(s["cash"], bot.START)
        self.assertFalse(s["dead"])

        # Save custom state
        s["cash"] = 42.0
        with open(self.state_file, "w") as f:
            json.dump(s, f)

        # Reload state
        s_reloaded = bot.load()
        self.assertEqual(s_reloaded["cash"], 42.0)

    def test_phi(self):
        # Phi(0) should equal 0.5
        self.assertAlmostEqual(bot.Phi(0), 0.5)

    def test_equity_calculation(self):
        s = {
            "cash": 30.0,
            "open": [
                {"shares": 10, "mark": 0.50, "price": 0.40},
                {"shares": 20, "price": 0.20} # fallback to price if no mark
            ]
        }
        # equity = 30 + (10 * 0.50) + (20 * 0.20) = 30 + 5 + 4 = 39.0
        self.assertEqual(bot.equity(s), 39.0)

    def test_is_yes_no(self):
        valid_market = {"outcomes": json.dumps(["Yes", "No"])}
        invalid_market = {"outcomes": json.dumps(["Team A", "Team B"])}
        self.assertTrue(bot.is_yes_no(valid_market))
        self.assertFalse(bot.is_yes_no(invalid_market))

    def test_scoreboard(self):
        s = {
            "scores": [
                {"q": "Q1", "model": 0.8, "mkt": 0.7, "y": 1},
                {"q": "Q2", "model": 0.2, "mkt": 0.3, "y": 0}
            ]
        }
        res = bot.scoreboard(s)
        self.assertIn("Scoreboard: 2 resolved", res)
        self.assertIn("Brier model 0.0400", res)


if __name__ == "__main__":
    unittest.main()
