"""Apply `guardrails/<target>.yaml` to Bedrock, so the policy lives in git rather than in a
console.

    python scripts/guardrail.py show    guardrails/medibot.yaml   # render, call nothing
    python scripts/guardrail.py apply   guardrails/medibot.yaml   # create or update DRAFT
    python scripts/guardrail.py publish guardrails/medibot.yaml   # freeze DRAFT as a version

Bedrock edits always land on DRAFT. `publish` snapshots it as an immutable numbered
version, which is what the pipeline should point at: after that, editing this file and
applying changes nothing that is running until a new version is published and the file's
`version:` is moved to it.

`--observe` applies the same policies with every action set to NONE and grounding
thresholds at zero: nothing blocks, but the scores still come back. That is what makes a
threshold measurable rather than guessed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import boto3
import yaml


def _sensitive(strength: str) -> str:
    """Maximum sensitivity, except where the filter does not apply on that side."""
    return "NONE" if strength == "NONE" else "HIGH"


def build(spec: dict, *, observe: bool = False) -> dict:
    """The CreateGuardrail request body this YAML describes."""
    content_action = "NONE" if observe else "BLOCK"
    pii_action = "NONE" if observe else None  # None means "whatever the entry asks for"

    body: dict = {
        "blockedInputMessaging": spec["blocked_input_message"],
        "blockedOutputsMessaging": spec["blocked_output_message"],
        "contentPolicyConfig": {
            "tierConfig": {"tierName": spec.get("tier", "CLASSIC")},
            "filtersConfig": [
                {
                    "type": f["type"],
                    # Observe at maximum sensitivity: the configured strength can only
                    # show what it already catches. NONE stays NONE — the filter does not
                    # apply on that side at all.
                    "inputStrength": _sensitive(f["input"]) if observe else f["input"],
                    "outputStrength": _sensitive(f["output"]) if observe else f["output"],
                    "inputAction": content_action,
                    "outputAction": content_action,
                }
                for f in spec["content_filters"]
            ]
        },
        "topicPolicyConfig": {
            # Standard tier detects more reliably and raises the definition limit from 200
            # characters to 1000, which is what makes a definition in AWS's recommended
            # form fit at all. It requires cross-Region inference, set below.
            "tierConfig": {"tierName": spec.get("tier", "CLASSIC")},
            "topicsConfig": [
                {
                    "name": t["name"],
                    "type": t["type"],
                    "definition": t["definition"],
                    "examples": t["examples"],
                    # A topic can apply to one side only; see the config for which and
                    # why.
                    "inputEnabled": "input" in t.get("sides", ["input", "output"]),
                    "outputEnabled": "output" in t.get("sides", ["input", "output"]),
                    "inputAction": content_action,
                    "outputAction": content_action,
                }
                for t in spec["topics"]
            ]
        },
        "sensitiveInformationPolicyConfig": {
            "piiEntitiesConfig": [
                {
                    "type": e["type"],
                    "action": pii_action or e["action"],
                    # The risk runs outward: naming a patient is asking, not leaking.
                    "inputEnabled": False,
                    "outputEnabled": True,
                }
                for e in spec["pii_entities"]
            ],
            "regexesConfig": [
                {
                    "name": r["name"],
                    "pattern": r["pattern"],
                    "description": r["description"],
                    "action": pii_action or r["action"],
                    "inputEnabled": False,
                    "outputEnabled": True,
                }
                for r in spec["pii_regexes"]
            ],
        },
        "contextualGroundingPolicyConfig": {
            "filtersConfig": [
                {
                    "type": g["type"],
                    # Zero blocks nothing; the score still comes back.
                    "threshold": 0.0 if observe else g["threshold"],
                    "action": "BLOCK",
                    "enabled": True,
                }
                for g in spec["grounding"]
            ]
        },
    }
    if spec.get("cross_region_profile"):
        body["crossRegionConfig"] = {"guardrailProfileIdentifier": spec["cross_region_profile"]}
    return body


def find_by_name(client, name: str) -> dict | None:
    """The guardrail with this name, or None. Names are unique per account and region."""
    paginator = client.get_paginator("list_guardrails")
    for page in paginator.paginate():
        for summary in page["guardrails"]:
            if summary["name"] == name:
                return summary
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["apply", "show", "publish"])
    parser.add_argument("spec", type=Path)
    parser.add_argument("--observe", action="store_true",
                        help="apply with nothing blocking, so scores can be read")
    parser.add_argument("--name", help="override the guardrail name in the file")
    args = parser.parse_args()

    spec = yaml.safe_load(args.spec.read_text())
    name = args.name or spec["name"]
    body = build(spec, observe=args.observe)

    if args.action == "show":
        print(json.dumps({"name": name, **body}, indent=2))
        return 0

    client = boto3.client("bedrock", region_name=spec["region"])
    existing = find_by_name(client, name)

    if args.action == "publish":
        if not existing:
            raise SystemExit(f"{name} does not exist yet; apply it first")
        result = client.create_guardrail_version(
            guardrailIdentifier=existing["id"],
            description=f"Published from {args.spec.name}",
        )
        version = result["version"]
        print(f"published {name} ({existing['id']}) as version {version}")
        print(f"  pin it by setting `version: \"{version}\"` in {args.spec}")
        print("  DRAFT keeps moving; this version does not")
        return 0

    posture = "observe (nothing blocks)" if args.observe else "enforcing"

    if existing:
        result = client.update_guardrail(
            guardrailIdentifier=existing["id"],
            name=name,
            description=spec.get("description", f"Built from {args.spec.name}."),
            **body,
        )
        print(f"updated {name} ({result['guardrailId']}) v{result['version']} — {posture}")
    else:
        result = client.create_guardrail(
            name=name,
            description=spec.get("description", f"Built from {args.spec.name}."),
            **body,
        )
        print(f"created {name} ({result['guardrailId']}) v{result['version']} — {posture}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
