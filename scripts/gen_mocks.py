"""Generate 20 mock user JSON responses for the Trae Enterprise API.

Each file is named <email_safe>.json where email_safe is the email with
'@' replaced by '_at_' and '.' replaced by '_'. The schema mirrors the
real `/openapi/v1/statistics/user-model-usage` response and includes 2-4
model_usage entries per user.

Run this script any time you want to regenerate the fixtures:

    python scripts/gen_mocks.py            # default 20 users, seed=42
    python scripts/gen_mocks.py --count 5  # generate N users instead
    python scripts/gen_mocks.py --seed 0   # different RNG seed

The output is deterministic for a given seed so test runs stay stable.
"""
from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timezone
from pathlib import Path

# Project layout: this file lives in <root>/scripts/gen_mocks.py
ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "mock_responses"

ACCOUNTS = [f"user{i:02d}@company.com" for i in range(1, 21)]

MODELS = [
    "Doubao-Seed-2.0-Code",
    "Doubao-Seed-Code",
    "DeepSeek-V4-Pro",
    "Kimi-K2.6",
    "GLM-5",
]

# Mix of model_type values: CUE and Chat
MODEL_TYPES = ["CUE", "Chat"]


def email_safe(email: str) -> str:
    """Convert an email to a filesystem-safe slug used by the mock loader."""
    return email.replace("@", "_at_").replace(".", "_")


def make_payload(email: str, rng: random.Random) -> dict:
    """Build a mock API response for a single user."""
    n_models = rng.randint(2, 4)
    chosen = rng.sample(MODELS, n_models)
    model_usage = []
    for model_name in chosen:
        model_type = rng.choice(MODEL_TYPES)
        in_tokens = rng.randint(1_000, 500_000)
        out_tokens = rng.randint(1_000, 500_000)
        model_usage.append(
            {
                "model_name": model_name,
                "model_type": model_type,
                "model_source": "Trae",
                "usage": {
                    "input_tokens": in_tokens,
                    "output_tokens": out_tokens,
                },
            }
        )
    return {
        "code": 0,
        "message": "success",
        "request_id": f"req_mock_{email_safe(email)}",
        "data": {
            "items": [
                {
                    "email": email,
                    "model_usage": model_usage,
                }
            ]
        },
        "_meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "scripts/gen_mocks.py",
        },
    }


def write_one(email: str, rng: random.Random, out_dir: Path) -> Path:
    payload = make_payload(email, rng)
    target = out_dir / f"{email_safe(email)}.json"
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--count",
        type=int,
        default=len(ACCOUNTS),
        help="Number of accounts to generate (default: 20).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducible generation.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=OUT_DIR,
        help="Output directory (default: data/mock_responses).",
    )
    args = parser.parse_args()

    rng = random.Random(args.seed)
    args.out.mkdir(parents=True, exist_ok=True)

    accounts = ACCOUNTS[: args.count]
    written: list[Path] = []
    for email in accounts:
        written.append(write_one(email, rng, args.out))

    print(f"Wrote {len(written)} mock files to {args.out}")
    for p in written:
        print(f"  - {p.name}")


if __name__ == "__main__":
    main()
