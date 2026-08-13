"""Train and save the SwingTrend-01 research artifact."""

import argparse
import json
from pathlib import Path

from ttc_model import load_bars, load_config, save_artifact, train_model


def main() -> None:
    parser = argparse.ArgumentParser(description="Train TTC SwingTrend-01")
    parser.add_argument("--input", required=True, help="Chronological OHLCV CSV")
    parser.add_argument("--output", required=True, help="Destination .joblib artifact")
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    args = parser.parse_args()
    artifact = train_model(load_bars(args.input), load_config(args.config))
    save_artifact(artifact, args.output)
    print(json.dumps({"artifact": args.output, "metrics": artifact["metrics"]}, indent=2))


if __name__ == "__main__":
    main()
