import unittest

from core.chart import downsample, sparkline
from core.market_data import get_kline_data


class FakeBybit:
    def get_kline(self, symbol, interval, limit):
        del symbol, interval, limit
        # Newest first.  At server time 10 minutes, the candle starting at
        # minute 9 is still open and must be dropped for a 3-minute interval.
        return {
            "time": 600_000,
            "result": {
                "list": [
                    ["540000", "1", "2", "1", "2", "1"],
                    ["360000", "1", "2", "1", "2", "1"],
                    ["180000", "1", "2", "1", "2", "1"],
                    ["0", "1", "2", "1", "2", "1"],
                ]
            },
        }


class MarketAndChartTests(unittest.TestCase):
    def test_open_candle_is_excluded(self):
        rows = get_kline_data(FakeBybit(), "BTCUSDT", "3", 10)
        self.assertEqual([row["timestamp"] for row in rows], [0, 180000, 360000])

    def test_sparkline_is_bounded_and_ordered(self):
        chart = sparkline([1, 2, 3, 4], width=4)
        self.assertEqual(len(chart), 4)
        self.assertEqual(chart[0], "▁")
        self.assertEqual(chart[-1], "█")

    def test_downsample_has_requested_width(self):
        self.assertEqual(len(downsample(list(range(100)), 20)), 20)


if __name__ == "__main__":
    unittest.main()
