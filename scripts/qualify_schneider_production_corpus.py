from __future__ import annotations

import argparse
from pathlib import Path

from devagent.plc.schneider_production_readiness import (
    qualify_schneider_production_corpus,
    write_report,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Qualify a signed, tamper-evident real Schneider Control Expert export corpus "
            "against the merged V1-V9 deterministic theorem stack."
        )
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--corpus-root", type=Path)
    parser.add_argument(
        "--trust-store",
        type=Path,
        help=(
            "Operator-approved DevAgent Ed25519 trust store. Required when the corpus contains "
            "REAL_CONTROL_EXPERT_EXPORT cases."
        ),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path(".devagent/schneider-production-corpus-qualification.json"),
    )
    parser.add_argument(
        "--markdown",
        type=Path,
        default=Path(".devagent/schneider-production-corpus-qualification.md"),
    )
    parser.add_argument(
        "--allow-pending",
        action="store_true",
        help="Return success when the authenticated manifest is valid but required real-export families are still missing.",
    )
    parser.add_argument(
        "--single-pass",
        action="store_true",
        help="Debug only: skip the second deterministic analysis pass. Do not use for commercial qualification.",
    )
    args = parser.parse_args()

    result = qualify_schneider_production_corpus(
        args.manifest,
        corpus_root=args.corpus_root,
        trust_store_path=args.trust_store,
        run_twice=not args.single_pass,
    )
    write_report(result, json_path=args.report, markdown_path=args.markdown)

    print(f"Schneider production corpus status: {result.status}")
    print(f"Commercial static ready: {result.commercial_static_ready}")
    print(f"Manifest signature verified: {result.manifest_signature_verified}")
    print(f"Manifest signer: {result.manifest_signer_key_id or 'none'}")
    print(f"Real export cases: {result.real_export_cases}")
    print(f"Covered families: {', '.join(result.covered_real_families) or 'none'}")
    print(f"Missing families: {', '.join(result.missing_real_families) or 'none'}")
    print(f"JSON report: {args.report}")
    print(f"Markdown report: {args.markdown}")

    if result.status == "STATIC_CORPUS_QUALIFIED":
        return 0
    if result.status == "PENDING_REAL_EXPORT_CORPUS" and args.allow_pending:
        return 0
    return 2 if result.status == "PENDING_REAL_EXPORT_CORPUS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
