import json
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

MODEL_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODEL_DIRECTORY))
from ttc_model import FEATURE_COLUMNS, build_features, build_labels, chronological_split, validate_bars  # noqa: E402


def sample_bars(rows: int = 100) -> pd.DataFrame:
    index = np.arange(rows, dtype=float)
    close = 100 + index * 0.15 + np.sin(index / 4)
    return pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=rows, freq="h", tz="UTC"),
        "open": close - 0.05,
        "high": close + 0.40,
        "low": close - 0.40,
        "close": close,
        "volume": 1_000_000 + index * 1_000,
    })


class ValidationTests(unittest.TestCase):
    def test_rejects_duplicate_timestamps(self) -> None:
        bars = sample_bars()
        bars.loc[1, "timestamp"] = bars.loc[0, "timestamp"]
        with self.assertRaisesRegex(ValueError, "unique"):
            validate_bars(bars)

    def test_rejects_invalid_high(self) -> None:
        bars = sample_bars()
        bars.loc[5, "high"] = bars.loc[5, "low"] - 1
        with self.assertRaisesRegex(ValueError, "High"):
            validate_bars(bars)


class FeatureTests(unittest.TestCase):
    def test_all_features_are_available_after_warmup(self) -> None:
        latest = build_features(sample_bars()).iloc[-1]
        self.assertFalse(latest.loc[list(FEATURE_COLUMNS)].isna().any())

    def test_features_do_not_change_when_future_rows_are_appended(self) -> None:
        bars = sample_bars(100)
        first = build_features(bars.iloc[:80]).iloc[-1]
        second = build_features(bars).iloc[79]
        pd.testing.assert_series_equal(first.loc[list(FEATURE_COLUMNS)], second.loc[list(FEATURE_COLUMNS)], check_names=False)


class LabelTests(unittest.TestCase):
    def test_labels_only_complete_future_horizons(self) -> None:
        bars = sample_bars()
        labeled = build_labels(bars, build_features(bars), 10, 1.5, 1.0)
        self.assertLessEqual(len(labeled), len(bars) - 10)
        self.assertTrue(set(labeled["label"]).issubset({"LONG", "SHORT", "NO_TRADE"}))


class SplitTests(unittest.TestCase):
    def test_chronological_split_preserves_order(self) -> None:
        dataset = pd.DataFrame({"timestamp": pd.date_range("2025-01-01", periods=100, freq="h", tz="UTC")})
        split = chronological_split(dataset, 0.6, 0.2)
        self.assertEqual((len(split.train), len(split.calibration), len(split.test)), (60, 20, 20))
        self.assertLess(split.train["timestamp"].max(), split.calibration["timestamp"].min())
        self.assertLess(split.calibration["timestamp"].max(), split.test["timestamp"].min())


class ConfigurationTests(unittest.TestCase):
    def test_config_identifies_reference_member(self) -> None:
        with (MODEL_DIRECTORY / "config.json").open(encoding="utf-8") as handle:
            config = json.load(handle)
        self.assertEqual(config["member_id"], "TTC-SWING-TREND-01")
        self.assertEqual(config["forecast_horizon_bars"], 10)


if __name__ == "__main__":
    unittest.main()
