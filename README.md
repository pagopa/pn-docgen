# pn-docgen

Deterministic AWS component diagrams: CloudFormation discovery → inventory →
component graph → D2/ELK → normalized SVG. No AI participates in layout.

This is the local pre-PR implementation, not a published release. The public
suite is self-contained; private captures and experimental outputs are not
part of the source distribution. There is no automatic publishing workflow.

The first-version scope is **L3, one AWS account/region per capture, ECS and
Lambda components**. Use the CFN discovery mode. Legacy tag/both discovery and
L2 are available for compatibility, not covered by this release's acceptance.

## Install from source

Requires Python 3.11+ and **D2 0.7.1**. Run these commands from this repository:

```sh
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-runtime.lock
python -m pip install --no-deps -e .
python scripts/download_aws_icons.py
d2 --version
```

The icon downloader uses an immutable upstream commit and verifies all 21
SHA-256 hashes. Icons are downloaded only by that explicit setup command, not
by diagram generation. Their artwork is separately licensed; preserve NOTICE.
Install D2 0.7.1 from its official release for your platform, rather than relying
on the moving latest package-manager version. Linux ARM64 and macOS ARM64 have
been exercised locally; other platforms need their own acceptance run.

## Generate from a saved capture — no AWS calls

Use new output paths: existing inventories, graphs and diagrams are not overwritten.

```sh
pn-docgen resolve --inventory /private/path/inventory.json \
  --config /path/to/central/pndocgen.yaml --output /private/new/graph.json

pn-docgen generate --graph /private/new/graph.json \
  --config /path/to/central/pndocgen.yaml \
  --output-dir /private/new/diagrams --level l3 \
  --detail-level simplified --theme white --render
```

For per-resource connections use `--detail-level detailed`. ECS simplified
keeps the resources but aggregates connections between groups, preserving
their labels. Lambda simplified also aggregates cross-group connections and
arranges independent groups as compact grids. Within-group connections remain
node-level, with a column layout instead of a grid for that group. All selected
resources remain visible; original relationships are traced in the view report.
`--component` selects an exact component name, not a prefix.

## Live capture — AWS read APIs

Only run after verifying and authorizing the profile, region and component.
SSO login and authorization are the operator's responsibility. No deployment
or AWS resource mutations are part of the tool.

```sh
pn-docgen run --profile YOUR_READONLY_PROFILE --region YOUR_REGION \
  --component YOUR_COMPONENT --discovery-source cfn \
  --config /path/to/central/pndocgen.yaml \
  --output-dir /private/new/capture --level l3 \
  --detail-level simplified --theme white --render
```

Use a read-only role granting the read APIs needed by CFN, tagging, Lambda,
ECS, IAM, API Gateway, EventBridge and SNS enrichments. A denied enrichment is
recorded in `discovery_issues`: a diagram with warnings is a **partial map**.
An explicitly selected component with no discovered resources fails, preserving
the empty capture for diagnosis. Generation producing no diagrams also fails.

`--repo-path` optionally enriches from local CloudFormation. This is static
source evidence, not proof that the local revision is deployed. Review the
source hashes and unresolved bindings in the resolve report.

## Configuration and visibility

One central YAML can serve all component repositories; repository-local files
are not required. Explicit `--config` takes precedence and a missing explicit
file is an error. See [CONFIGURATION.md](CONFIGURATION.md).

```yaml
render:
  theme: white
  view:
    exclusions:
      - component: pn-delivery
        resource_type: lambda
        name_prefix: pn-delivery-versioning-
    edge_exclusions:
      - component: pn-mandate
        edge_type: dynamodb_stream
```

Security and Misc nodes are centrally omitted from the diagram in this
essential view. They are not erased from saved graphs. Unknown types can be
omitted through Misc even if operationally important, for example an
unclassified Pipe: inspect the view report. Schedules are classified as Rules
and remain visible. Support objects such as API models and policies are filtered
by the shared classification registry.

## Output and interpretation

- `.raw.json`: private CFN resource records before inventory conversion; not a
  recording of every enrichment response.
- Inventory JSON and graph JSON: reusable snapshots for offline generation.
- `.resolve.json`: input accounting and static-source diagnostics.
- `.d2`, relative `assets/`, `.view.json`: diagram source and view decisions.
- `.svg`: standalone rendered image with embedded icons and attribution.

Relationships may describe IAM permissions, configuration bindings, environment
references or same-component heuristics. They are **not observed traffic**.
Evidence is available where known; historical and some static relationships
may have no evidence. Omitted external endpoints are reported, not fabricated.
More than one possible ECS owner causes an explicit ambiguity error.

Keep live-derived captures, reports and images private. `.gitignore` is a guard,
not a sanitizer: never force-add live artifacts or publish them automatically.

## Tests and packaging

```sh
python -m pip install pytest==8.3.5 setuptools==75.8.0 wheel==0.45.1
python -m pytest tests -q -p no:cacheprovider
python scripts/build_release.py --output-dir /new/path/to/wheels
```

The default suite uses synthetic inputs, blocks Python sockets/AWS clients and
does not require private captures. Private tests are explicitly skipped unless
`--private-root=/path/to/private/checkout` is supplied. Current render acceptance
also requires `--private-baselines=/path/to/versioned/acceptance` with its hashed
manifest. Exact historical geometry comparisons require `--legacy-goldens`:
they are diagnostic, not the current layout contract. Changes from historical
goldens include corrected semantic labels, Scheduler visibility, text-aware
container fitting and bounded port centering. Do not publish private fixtures
or silently regenerate old goldens. Public synthetic tests remain runnable
without any of these private directories.

Before distributing a wheel, verify its contents: package code, four templates,
21 unchanged icons, icon NOTICE and project LICENSE; no captures/experiments.
Build after downloading the verified icons. An installation without icons has
a rectangle fallback, not the accepted AWS-icon visual style.
The build helper checks every icon hash and requires a new output directory;
it uses installed build dependencies with no package index or persistent cache.
It may create standard build/egg-info files in the source checkout.

With the same pinned D2 binary, inputs and configuration, repeated and
order-inverted generation is tested byte-for-byte. D2 builds may record
`0.7.1` or `v0.7.1` in the SVG version attribute: this metadata difference does
not change geometry. Pin the actual platform binary for stable byte diffs.

## PNG export

PNG is rasterized from the final normalized SVG using optional resvg-py 0.5.0
(Rust resvg 0.48.1). Install with `pip install '.[raster]'`, then use
`--render --render-format png`. Both the canonical SVG and PNG are saved.
Only fonts embedded by D2 are used; system fonts are disabled. The PNG keeps
icon attribution and the source SVG SHA-256 in its metadata. Rendering is
local; there are no font downloads. Images over 40 megapixels and unsupported
embedded text fail explicitly. Existing output files are never overwritten.

## Licenses

Project code: [EUPL 1.2](LICENSE), matching the PN repositories selected by the
project owner. AWS icon artwork is separate; see
[the icon NOTICE](pndocgen/engine/d2/icons/aws/NOTICE.txt). Keep attribution with
redistributed assets and diagrams. The project license does not relicense the
artwork or authorize publication of private deployment data.

AWS explicitly permits architecture-diagram use on its
[Architecture Icons page](https://aws.amazon.com/architecture/icons/).
For our pinned icon copies, the upstream
[License Summary](https://github.com/awslabs/aws-icons-for-plantuml/blob/e26e2c05daf8b6bc4c764669fc2be04c314ccb8c/README.md#license-summary)
declares CC BY-ND 2.0 for artwork, separately from MIT for upstream code.
See the NOTICE for the exact revision, license text and redistribution conditions.
A NOTICE alone is not blanket permission: preserve attribution and licensing,
and do not treat the artwork as freely modifiable. Attribution is provided in
the NOTICE and embedded SVG/PNG metadata, without visible diagram credits or
layout changes. Preserve those notices when distributing outputs and check
the license conditions for the intended use.
