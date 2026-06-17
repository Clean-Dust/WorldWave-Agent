"""
pretrain_seed.py — seed weight pre-training script (Cold Start Seed Weights)

At release, perform high-intensity pre-training on the subconscious model, so that users have a high level of intuition from the first second of loading, and subsequent P2P is only for long-tail effects and the latest framework updates.

Training data: synthetically generate common programming error modes (Synthetic Data)
Does not involve any real user code. Purely based on common engineering statistics knowledge.

Usage:
  cd ~/worldwave
  python scripts/pretrain_seed.py [--trees 20] [--output model.json]
"""

import argparse
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.predictor import RandomForest
from core.features import PADDED_FEATURES


def generate_synthetic_data(n_samples: int = 500) -> tuple:
    """
    Generate synthetic training data.

    Each (X, y):
      X = 32-dimensional feature vector (15 actual + 17 reserved padding 0, ** normalize** [0, 1] scope)
      y = 0.0 (success) / 1.0 (fault)

    Directly generate normalize value to avoid FEATURE_RANGES scope compress issues.

    mode 1: consecutive errors → fault (feature 0)
      3+ consecutive errors → 80% probability of fault
      5+ consecutive errors → 95% probability of fault

    mode 2: tool loop → fault (feature 1)
      Same tool 4+ consecutive times → 70% probability of fault

    mode 3: high latency + empty response → fault (feature 2, 10)
      API latency > 0.5 (norm) + LLM empty response → 90% fault

    mode 4: long no checkpoint → risk increase (feature 11)
      No checkpoint value > 0.3 (norm) each +0.1 risk +15%

    mode 5: efficient mode (reward)
      Low latency + many tools + no error → 95% success
    """
    X, y = [], []

    for _ in range(n_samples):
        vec = [0.0] * PADDED_FEATURES  # 32 dimensions (reserved padding slots)

        mode = random.choices(
            ["error_spiral", "tool_loop", "latency_crash",
             "checkpoint_stale", "efficient", "noise"],
            weights=[0.25, 0.2, 0.2, 0.15, 0.15, 0.05], k=1
        )[0]

        # Random provider one-hot (increase diversity)
        provider_choice = random.choices(
            [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
            weights=[0.4, 0.35, 0.25], k=1
        )[0]
        vec[12], vec[13], vec[14] = provider_choice

        if mode == "error_spiral":
            vec[0] = random.uniform(0.3, 0.9)  # consecutive errors (norm 0.3-0.9)
            vec[7] = 0.0                         # one step failed
            vec[2] = random.uniform(0.1, 0.3)    # medium latency
            outcome = 0.8 + random.uniform(0, 0.2)

        elif mode == "tool_loop":
            vec[1] = random.uniform(0.5, 0.9)   # tool loop (norm 0.5-0.9)
            vec[8] = random.uniform(0.0, 0.15)  # low diversity (norm)
            vec[7] = random.randint(0, 1) * 1.0
            outcome = 0.65 + random.uniform(0, 0.25)

        elif mode == "latency_crash":
            vec[2] = random.uniform(0.5, 0.95)  # high latency (norm 0.5-0.95)
            vec[10] = 1.0                         # LLM empty response
            vec[3] = 1.0                          # latency increase
            outcome = 0.85 + random.uniform(0, 0.15)

        elif mode == "checkpoint_stale":
            vec[11] = random.uniform(0.3, 0.9)  # long without checkpoint
            vec[0] = random.uniform(0.1, 0.3)
            outcome = min(0.9, 0.15 + vec[11] * 0.6)

        elif mode == "efficient":
            vec[2] = random.uniform(0.0, 0.1)   # low latency
            vec[8] = random.uniform(0.3, 0.8)   # high tool diversity
            vec[0] = 0.0                          # no error
            vec[7] = 1.0                          # one step success
            vec[10] = 0.0                         # non-empty response
            vec[11] = random.uniform(0.0, 0.1)    # recent checkpoint
            outcome = random.uniform(0, 0.15)

        else:  # noise
            for i in range(12):
                vec[i] = random.uniform(0, 1)
            outcome = random.uniform(0, 1)

        # Add a small amount of random noise
        for i in range(12):
            vec[i] += random.gauss(0, 0.03)
            vec[i] = max(0.0, min(1.0, vec[i]))

        vec[5] = random.uniform(0.01, 0.2)   # spiral completion count
        vec[4] = random.uniform(0.0, 0.1)    # token consumption rate
        vec[9] = random.uniform(0.0, 0.2)    # memory recall
        vec[6] = random.randint(0, 5) / 5.0  # when  phase

        X.append(vec)
        y.append(max(0.0, min(1.0, outcome)))

    return X, y


def main():
    parser = argparse.ArgumentParser(
        description="Pre-training seed subconscious model"
    )
    parser.add_argument("--trees", type=int, default=20,
                        help="Number of random forest trees (default: 20)")
    parser.add_argument("--samples", type=int, default=2000,
                        help="Number of synthetic training samples (default: 2000)")
    parser.add_argument("--depth", type=int, default=5,
                        help="Maximum depth (default: 5)")
    parser.add_argument("--output", type=str, default="",
                        help="outputpath (default: ~/worldwave/data/subconscious/model.json)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed")
    args = parser.parse_args()

    random.seed(args.seed)

    ww_home = os.environ.get("WW_HOME", os.path.expanduser("~/worldwave"))
    output_path = args.output or os.path.join(ww_home, "data", "subconscious", "model.json")

    print("🌱 Seed weight pre-training")
    print(f"   Number of trees: {args.trees}")
    print(f"   Number of samples: {args.samples}")
    print(f"   Maximum depth: {args.depth}")
    print()

    # Generate synthetic data
    print("📊 Generating synthetic training data...")
    start = time.time()
    X, y = generate_synthetic_data(args.samples)
    pos = sum(y)
    print(f"   Completed: {len(X)} sample "
          f"(fault={int(pos)}/{int(len(y)-pos)}) "
          f"at  {time.time()-start:.1f}s")

    # Data at [0,1] scope, no extra normalize needed
    X_norm = X

    # training Random Forest
    print("🌲 training Random Forest...")
    start = time.time()
    rf = RandomForest(
        n_trees=args.trees,
        max_depth=args.depth,
        min_samples_leaf=3,
    )
    rf.fit(X_norm, y)
    print(f"   complete: {len(rf.trees)}  tree, "
          f"size={rf.model_size()}, "
          f"OOBerror={sum(rf.oob_errors)/max(1,len(rf.oob_errors)):.3f} "
          f"at  {time.time()-start:.1f}s")

    # save
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        f.write(rf.to_json())
    print(f"💾 saveto : {output_path}")
    print(f"   size: {rf.model_size()}")
    print()

    # Quick validate
    print("🔍 fastvalidate:")
    from core.features import FeatureExtractor
    fe = FeatureExtractor()
    # 15-dimensional testvector → autopadding to 32 dimensions
    def p15(v15):
        return v15 + [0.0] * (PADDED_FEATURES - len(v15))
    test_cases = [
        ("consecutive errors x5", p15([0.5, 0.0, 0.1, 0.5, 0.002, 0.05, 0.6, 0.0, 0.1, 0.05, 0.0, 0.03, 1.0, 0.0, 0.0]), 0.5),
        ("tool loop x4", p15([0.0, 0.6, 0.03, 0.5, 0.004, 0.05, 0.4, 0.0, 0.1, 0.03, 0.0, 0.01, 0.0, 1.0, 0.0]), 0.4),
        ("high latency+empty response", p15([0.0, 0.0, 0.8, 1.0, 0.02, 0.05, 0.0, 0.0, 0.3, 0.03, 1.0, 0.08, 0.0, 0.0, 1.0]), 0.6),
        ("efficientmode", p15([0.0, 0.0, 0.01, 0.5, 0.001, 0.1, 0.0, 1.0, 0.5, 0.05, 0.0, 0.01, 1.0, 0.0, 0.0]), 0.15),
        ("long   no  checkpoint", p15([0.15, 0.0, 0.05, 0.5, 0.002, 0.05, 0.6, 1.0, 0.17, 0.03, 0.0, 0.7, 0.0, 1.0, 0.0]), 0.4),
    ]

    for name, norm_vec, min_expected in test_cases:
        pred = rf.predict(norm_vec)
        status = "✅" if pred >= min_expected else "⚠️"
        print(f"   {status} {name}: pred={pred:.2f} (expectation>{min_expected})")

    print()
    print("🎉 seedweightpre-trainingcomplete!")


if __name__ == "__main__":
    main()
