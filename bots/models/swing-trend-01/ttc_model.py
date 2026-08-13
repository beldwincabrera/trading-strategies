"""Research reference implementation for TTC Member SwingTrend-01."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import RandomForestClassifier
from sklearn.frozen import FrozenEstimator
from sklearn.metrics import accuracy_score, balanced_accuracy_score, classification_report

CLASSES = ("LONG", "NO_TRADE", "SHORT")
REQUIRED_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")
FEATURE_COLUMNS = (
    "return_1", "return_5", "return_20", "ema_gap_fast", "ema_gap_slow",
    "ema_alignment", "slope_10", "atr_pct", "range_position_20", "volume_ratio_20",
)


@dataclass(frozen=True)
class DatasetSplit:
    train: pd.DataFrame
    calibration: pd.DataFrame
    test: pd.DataFrame


def load_config(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_bars(path: str | Path) -> pd.DataFrame:
    return validate_bars(pd.read_csv(path))


def validate_bars(frame: pd.DataFrame) -> pd.DataFrame:
    missing = sorted(set(REQUIRED_COLUMNS) - set(frame.columns))
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(missing)}")
    result = frame.loc[:, REQUIRED_COLUMNS].copy()
    result["timestamp"] = pd.to_datetime(result["timestamp"], utc=True, errors="raise")
    result = result.sort_values("timestamp").reset_index(drop=True)
    if result["timestamp"].duplicated().any():
        raise ValueError("Timestamps must be unique")
    prices = ["open", "high", "low", "close"]
    if result[[*prices, "volume"]].isna().any().any():
        raise ValueError("OHLCV values cannot be missing")
    if (result[prices] <= 0).any().any():
        raise ValueError("Prices must be positive")
    if (result["volume"] < 0).any():
        raise ValueError("Volume cannot be negative")
    if (result["high"] < result[["open", "close", "low"]].max(axis=1)).any():
        raise ValueError("High must be greater than or equal to open, close, and low")
    if (result["low"] > result[["open", "close", "high"]].min(axis=1)).any():
        raise ValueError("Low must be less than or equal to open, close, and high")
    return result


def _atr(frame: pd.DataFrame, period: int) -> pd.Series:
    previous_close = frame["close"].shift(1)
    ranges = pd.concat([
        frame["high"] - frame["low"],
        (frame["high"] - previous_close).abs(),
        (frame["low"] - previous_close).abs(),
    ], axis=1)
    return ranges.max(axis=1).rolling(period, min_periods=period).mean()


def _rolling_slope(series: pd.Series, window: int) -> pd.Series:
    x = np.arange(window, dtype=float)
    centered_x = x - x.mean()
    denominator = np.square(centered_x).sum()

    def slope(values: np.ndarray) -> float:
        return float(np.dot(centered_x, values - values.mean()) / denominator / values[-1])

    return series.rolling(window, min_periods=window).apply(slope, raw=True)


def build_features(frame: pd.DataFrame, atr_period: int = 14) -> pd.DataFrame:
    bars = validate_bars(frame)
    close = bars["close"]
    ema_fast = close.ewm(span=10, adjust=False).mean()
    ema_slow = close.ewm(span=30, adjust=False).mean()
    rolling_high = bars["high"].rolling(20, min_periods=20).max()
    rolling_low = bars["low"].rolling(20, min_periods=20).min()
    atr = _atr(bars, atr_period)
    features = pd.DataFrame({"timestamp": bars["timestamp"], "close": close, "atr": atr})
    features["return_1"] = close.pct_change(1)
    features["return_5"] = close.pct_change(5)
    features["return_20"] = close.pct_change(20)
    features["ema_gap_fast"] = close / ema_fast - 1
    features["ema_gap_slow"] = close / ema_slow - 1
    features["ema_alignment"] = ema_fast / ema_slow - 1
    features["slope_10"] = _rolling_slope(close, 10)
    features["atr_pct"] = atr / close
    features["range_position_20"] = (close - rolling_low) / (rolling_high - rolling_low).replace(0, np.nan)
    features["volume_ratio_20"] = bars["volume"] / bars["volume"].rolling(20, min_periods=20).mean()
    return features


def build_labels(bars: pd.DataFrame, features: pd.DataFrame, horizon: int, up_atr: float, down_atr: float) -> pd.DataFrame:
    result = features.copy()
    labels: list[str | None] = []
    forward_returns: list[float | None] = []
    for index in range(len(bars)):
        if index + horizon >= len(bars) or pd.isna(result.at[index, "atr"]):
            labels.append(None)
            forward_returns.append(None)
            continue
        start, atr = float(bars.at[index, "close"]), float(result.at[index, "atr"])
        upper, lower = start + up_atr * atr, start - down_atr * atr
        label, exit_price = "NO_TRADE", float(bars.at[index + horizon, "close"])
        for future in range(index + 1, index + horizon + 1):
            high, low = float(bars.at[future, "high"]), float(bars.at[future, "low"])
            if high >= upper and low <= lower:
                exit_price = float(bars.at[future, "close"])
                break
            if high >= upper:
                label, exit_price = "LONG", upper
                break
            if low <= lower:
                label, exit_price = "SHORT", lower
                break
        labels.append(label)
        forward_returns.append(exit_price / start - 1)
    result["label"], result["forward_return"] = labels, forward_returns
    return result.dropna(subset=[*FEATURE_COLUMNS, "label", "forward_return"]).reset_index(drop=True)


def chronological_split(dataset: pd.DataFrame, train_fraction: float, calibration_fraction: float) -> DatasetSplit:
    if train_fraction <= 0 or calibration_fraction <= 0 or train_fraction + calibration_fraction >= 1:
        raise ValueError("Fractions must leave positive train, calibration, and test partitions")
    train_end = int(len(dataset) * train_fraction)
    calibration_end = train_end + int(len(dataset) * calibration_fraction)
    if train_end == 0 or calibration_end == train_end or calibration_end >= len(dataset):
        raise ValueError("Dataset is too small for the requested split")
    return DatasetSplit(dataset.iloc[:train_end].copy(), dataset.iloc[train_end:calibration_end].copy(), dataset.iloc[calibration_end:].copy())


def train_model(bars: pd.DataFrame, config: dict[str, Any]) -> dict[str, Any]:
    validated = validate_bars(bars)
    features = build_features(validated, int(config["atr_period"]))
    dataset = build_labels(validated, features, int(config["forecast_horizon_bars"]), float(config["up_barrier_atr"]), float(config["down_barrier_atr"]))
    split = chronological_split(dataset, float(config["train_fraction"]), float(config["calibration_fraction"]))
    if set(split.train["label"].unique()) != set(CLASSES):
        raise ValueError("Training partition must contain LONG, SHORT, and NO_TRADE labels")

    forest = RandomForestClassifier(random_state=int(config["random_state"]), **config["forest"])
    forest.fit(split.train.loc[:, FEATURE_COLUMNS], split.train["label"])
    calibrated = CalibratedClassifierCV(FrozenEstimator(forest), method="sigmoid")
    calibrated.fit(split.calibration.loc[:, FEATURE_COLUMNS], split.calibration["label"])
    predictions = calibrated.predict(split.test.loc[:, FEATURE_COLUMNS])
    metrics = {
        "accuracy": float(accuracy_score(split.test["label"], predictions)),
        "balanced_accuracy": float(balanced_accuracy_score(split.test["label"], predictions)),
        "classification_report": classification_report(split.test["label"], predictions, labels=list(CLASSES), output_dict=True, zero_division=0),
        "rows": {"total": len(dataset), "train": len(split.train), "calibration": len(split.calibration), "test": len(split.test)},
    }
    return {
        "member_id": config["member_id"],
        "model_version": config["model_version"],
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "feature_columns": list(FEATURE_COLUMNS),
        "classes": list(calibrated.classes_),
        "config": config,
        "model": calibrated,
        "metrics": metrics,
        "class_expected_returns": split.train.groupby("label")["forward_return"].mean().to_dict(),
        "split_boundaries": {
            "train_start": split.train["timestamp"].iloc[0].isoformat(),
            "train_end": split.train["timestamp"].iloc[-1].isoformat(),
            "calibration_end": split.calibration["timestamp"].iloc[-1].isoformat(),
            "test_end": split.test["timestamp"].iloc[-1].isoformat(),
        },
    }


def save_artifact(artifact: dict[str, Any], path: str | Path) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, destination)


def load_artifact(path: str | Path) -> dict[str, Any]:
    return joblib.load(path)


def _reason_codes(row: pd.Series) -> list[str]:
    candidates = [
        (abs(float(row["ema_alignment"])), "EMA_BULLISH_ALIGNMENT" if row["ema_alignment"] > 0 else "EMA_BEARISH_ALIGNMENT"),
        (abs(float(row["return_20"])), "POSITIVE_20_BAR_RETURN" if row["return_20"] > 0 else "NEGATIVE_20_BAR_RETURN"),
        (abs(float(row["slope_10"])), "POSITIVE_10_BAR_SLOPE" if row["slope_10"] > 0 else "NEGATIVE_10_BAR_SLOPE"),
        (abs(float(row["volume_ratio_20"] - 1)), "ABOVE_AVERAGE_VOLUME" if row["volume_ratio_20"] > 1 else "BELOW_AVERAGE_VOLUME"),
    ]
    return [reason for _, reason in sorted(candidates, reverse=True)[:3]]


def create_ballot(bars: pd.DataFrame, artifact: dict[str, Any]) -> dict[str, Any]:
    config = artifact["config"]
    features = build_features(validate_bars(bars), int(config["atr_period"]))
    eligible = features.dropna(subset=list(artifact["feature_columns"]))
    if eligible.empty:
        raise ValueError("Not enough completed bars to calculate all features")
    latest = eligible.iloc[-1]
    model = artifact["model"]
    sample = latest.loc[list(artifact["feature_columns"])].to_frame().T
    probabilities = dict(zip(model.classes_, model.predict_proba(sample)[0], strict=True))
    directional = max(("LONG", "SHORT"), key=lambda label: probabilities.get(label, 0.0))
    opposite = "SHORT" if directional == "LONG" else "LONG"
    direction_probability = float(probabilities.get(directional, 0))
    edge = direction_probability - float(probabilities.get(opposite, 0))
    if float(probabilities.get("NO_TRADE", 0)) >= direction_probability:
        decision, abstain_reason = "ABSTAIN", "NO_TRADE_IS_MOST_LIKELY"
    elif direction_probability < float(config["minimum_direction_probability"]):
        decision, abstain_reason = "ABSTAIN", "DIRECTION_PROBABILITY_BELOW_THRESHOLD"
    elif edge < float(config["minimum_probability_edge"]):
        decision, abstain_reason = "ABSTAIN", "DIRECTION_EDGE_BELOW_THRESHOLD"
    else:
        decision, abstain_reason = directional, None
    return {
        "member_id": artifact["member_id"],
        "model_version": artifact["model_version"],
        "decision_timestamp": latest["timestamp"].isoformat(),
        "direction": decision,
        "probabilities": {label: round(float(probabilities.get(label, 0)), 6) for label in CLASSES},
        "probability_edge": round(edge, 6),
        "expected_forward_return": round(float(artifact["class_expected_returns"].get(directional, 0)), 6),
        "forecast_horizon_bars": int(config["forecast_horizon_bars"]),
        "abstain_reason": abstain_reason,
        "reason_codes": _reason_codes(latest),
        "artifact_created_at_utc": artifact["created_at_utc"],
    }
