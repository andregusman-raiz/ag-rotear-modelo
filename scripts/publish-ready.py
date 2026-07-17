#!/usr/bin/env python3
"""Publish the canonical READY commitment for guardian inputs."""

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Optional, Sequence


def _guardian_module():
    path = Path(__file__).with_name("guarded-run.py")
    spec = importlib.util.spec_from_file_location("ag_model_router_guardian", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("guardian module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="publish-ready.py")
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--nonce", required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _guardian_module().publish_ready(args.input_dir, args.nonce)
        return 0
    except Exception:
        print("publisher error: READY publication failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
