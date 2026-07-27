#!/usr/bin/env bash
# Prove the routing golden evals. Usage: ./scripts/prove-routing.sh [--fallback]
cd "$(dirname "$0")/.."
python3 -c "import yaml" 2>/dev/null || pip3 install --quiet pyyaml
python3 eval/run_evals.py "$@"
