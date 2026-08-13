"""Generate one standardized SwingTrend-01 Council ballot."""

import argparse
import json

from ttc_model import create_ballot, load_artifact, load_bars


def main() -> None:
    parser = argparse.ArgumentParser(description="Run TTC SwingTrend-01 inference")
    parser.add_argument("--input", required=True, help="Recent OHLCV CSV")
    parser.add_argument("--model", required=True, help="Trained .joblib artifact")
    args = parser.parse_args()
    print(json.dumps(create_ballot(load_bars(args.input), load_artifact(args.model)), indent=2))


if __name__ == "__main__":
    main()
