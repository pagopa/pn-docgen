#!/usr/bin/env python3
"""
Download the minimal set of AWS architecture PNG icons needed by pn-docgen.

Source: https://github.com/awslabs/aws-icons-for-plantuml  (MIT licence)
Icons are free for use in architecture diagrams per AWS Trademark Guidelines.

Run once after cloning the repo (or when adding new resource types):
    python scripts/download_aws_icons.py

Use --force to re-download even if files already exist.
"""

import urllib.request
import sys
from pathlib import Path

# Base URL for the official AWS PlantUML icon set (main branch, dist folder)
BASE = "https://raw.githubusercontent.com/awslabs/aws-icons-for-plantuml/main/dist"

# icon_name → (plantuml_category, plantuml_filename_without_extension)
# File extension is always .png in the upstream repo.
ICONS: dict[str, tuple[str, str]] = {
    "aws-lambda":         ("Compute",                    "Lambda"),
    "aws-ecs":            ("Containers",                 "ElasticContainerService"),
    "aws-step-functions": ("ApplicationIntegration",     "StepFunctions"),
    "aws-glue":           ("Analytics",                  "Glue"),
    "aws-batch":          ("Compute",                    "Batch"),
    "aws-dynamodb":       ("Database",                   "DynamoDB"),
    "aws-s3":             ("Storage",                    "SimpleStorageService"),
    "aws-elasticache":    ("Database",                   "ElastiCache"),
    "aws-rds":            ("Database",                   "RDS"),
    "aws-opensearch":     ("Analytics",                  "OpenSearchService"),
    "aws-efs":            ("Storage",                    "EFS"),
    "aws-sqs":            ("ApplicationIntegration",     "SimpleQueueService"),
    "aws-sns":            ("ApplicationIntegration",     "SimpleNotificationService"),
    "aws-kinesis":        ("Analytics",                  "KinesisDataStreams"),
    "aws-eventbridge":    ("ApplicationIntegration",     "EventBridge"),
    "aws-api-gateway":    ("NetworkingContentDelivery",  "APIGateway"),
    "aws-alb":            ("NetworkingContentDelivery",  "ElasticLoadBalancingApplicationLoadBalancer"),
    "aws-nlb":            ("NetworkingContentDelivery",  "ElasticLoadBalancingNetworkLoadBalancer"),
    "aws-cloudfront":     ("NetworkingContentDelivery",  "CloudFront"),
    "aws-waf":            ("SecurityIdentityCompliance", "WAF"),
    "aws-cognito":        ("SecurityIdentityCompliance", "Cognito"),
}

# Destination: pndocgen/engine/d2/icons/aws/
# Icons are stored as <icon_name>.png  (e.g. aws-sqs.png)
DEST = Path(__file__).parent.parent / "pndocgen" / "engine" / "d2" / "icons" / "aws"


def download_icons(force: bool = False) -> None:
    DEST.mkdir(parents=True, exist_ok=True)

    ok = 0
    fail = 0
    skip = 0

    for icon_name, (category, stem) in ICONS.items():
        dest_file = DEST / f"{icon_name}.png"

        if dest_file.exists() and not force:
            print(f"  ✓ skip  {dest_file.name} (already present)")
            skip += 1
            continue

        url = f"{BASE}/{category}/{stem}.png"
        try:
            urllib.request.urlretrieve(url, dest_file)
            size = dest_file.stat().st_size
            print(f"  ↓ ok    {dest_file.name}  ({size} bytes)  ← {category}/{stem}.png")
            ok += 1
        except Exception as e:
            print(f"  ✗ fail  {dest_file.name}  ← {url}  ({e})", file=sys.stderr)
            fail += 1

    print(f"\nDone: {ok} downloaded, {skip} skipped, {fail} failed")
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    force = "--force" in sys.argv
    print(f"Downloading AWS icons → {DEST}\n")
    download_icons(force=force)
