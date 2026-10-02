#!/usr/bin/env python3
"""
Download the minimal set of AWS architecture PNG icons needed by pn-docgen.

Source: https://github.com/awslabs/aws-icons-for-plantuml
The repository's code is MIT-licensed, while its icon artwork is CC BY-ND 2.0.
See pndocgen/engine/d2/icons/aws/NOTICE.txt before packaging or publishing.

Run once after cloning the repo (or when adding new resource types):
    python scripts/download_aws_icons.py

Use --force to re-download even if files already exist.
"""

import hashlib
import sys
import urllib.request
from pathlib import Path

# All 21 PNG hashes verified against this immutable upstream commit.
UPSTREAM_REVISION = "e26e2c05daf8b6bc4c764669fc2be04c314ccb8c"
BASE = f"https://raw.githubusercontent.com/awslabs/aws-icons-for-plantuml/{UPSTREAM_REVISION}/dist"

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

# Frozen bytes of the 21 locally validated icons. Upstream `main` may move;
# downloading a different image must fail rather than silently change renders.
EXPECTED_SHA256 = {
    "aws-alb": "d9dd545eb3751975595194238fb9252072af8bb87a96b6fc3d5e2ccc031bb641",
    "aws-api-gateway": "c220cf32987bf162c2a6233589e7c39a259a45c33410b0bce0e7a4a8aa6e6eea",
    "aws-batch": "26f4e85aea11ce0778840d4126ab9a0663fc6d017613e1d00d9b1da5db7ba403",
    "aws-cloudfront": "b132df4f324863f9930a8a8de40afd4a706100d4f0cedaf301680e8dc72517d3",
    "aws-cognito": "3ce10fc1d162642cb45b3a9b4539b53ec60b9e91a621b43b10103fea3f10be66",
    "aws-dynamodb": "e15ee090401abeafdfc4a3e3a31280354d1783c4170074f692796be330265b32",
    "aws-ecs": "c3dcff02e52e0ec461882c45978ef80cfec1558c94ad2167707aa1fbad423c46",
    "aws-efs": "a13dbd244ae233a94d8e85b924d558eb5b6c12bb4f61da9472a1979bc516546b",
    "aws-elasticache": "3edbfc1009143858540540c8bb2fc9115050daa9c43d1762dd7d945381f078f5",
    "aws-eventbridge": "dd1b23d029e338ab13119f8617c19f578e3254ab2e91a76562d61833273b4426",
    "aws-glue": "fb52608b397a5fcdb4148bd1d041eea56e4ace4cf9492dadae26acf9146ffa63",
    "aws-kinesis": "c7a64108376afcaa81b0aaf57f27a286ff396370ff6d799c89110e931d921e2e",
    "aws-lambda": "c10101a20a279b47c66caaf8adb54aedaad4735a27ad54075e1d200a15af55e1",
    "aws-nlb": "890f76cba5bf755d47b964a9a6c33730180af44e3d89b2320e1df72c781d7d3b",
    "aws-opensearch": "b988524c5672b46be3ced87a42715437e9a87775c71e614e371a443591f0f84d",
    "aws-rds": "aca87b56f1a06382e52a010e5db040ca4338ce8d484f72c2f0f6e0a4c3e0c2be",
    "aws-s3": "6715951abe7d964792afc3d36dec4e2f89e7d27df74d702ba75e575304a80d22",
    "aws-sns": "e33968ebc85ce257f3f92608f385eaff09c2d501dcbcec44e8ee6f668259091a",
    "aws-sqs": "09f5e701a039a8101b6467224851db0bb752b33027c6c533621f9c100da306bb",
    "aws-step-functions": "1f2f49f53facc8a87362029d2045daa9847e0fe7872fe921b9fee74f7d51e666",
    "aws-waf": "b37a7763bcca498d9fe4594f16ec93d222a0456b31fc3653dd859f399827e64e",
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
            actual = hashlib.sha256(dest_file.read_bytes()).hexdigest()
            if actual != EXPECTED_SHA256[icon_name]:
                print(
                    f"  ✗ fail  {dest_file.name} (unexpected SHA-256: {actual})",
                    file=sys.stderr,
                )
                fail += 1
            else:
                print(f"  ✓ skip  {dest_file.name} (verified SHA-256)")
                skip += 1
            continue

        url = f"{BASE}/{category}/{stem}.png"
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                data = response.read()
            actual = hashlib.sha256(data).hexdigest()
            if actual != EXPECTED_SHA256[icon_name]:
                raise ValueError(f"SHA-256 mismatch for {url}: {actual}")
            dest_file.write_bytes(data)
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
