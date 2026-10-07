# Configuration: resources and connections in L3 diagrams

Use one centrally maintained `pndocgen.yaml` and select it explicitly with
`--config /path/to/pndocgen.yaml` on `discover`, `resolve`, `generate`, or `run`. The file
can live in pn-docgen or another local directory; a file in each microservice
repository is not required. There is no automatic merge of central and
per-repository files. `--repo-path` selects source templates for enrichment,
not a second configuration file. CLI options override the corresponding YAML
options where both are exposed, for example `--theme`.

The application loader searches the current working directory for
`pndocgen.yaml` or `pndocgen.yml` when no path is supplied. Use an explicit
`--config` consistently across stages: the resolver also reads its raw rules
from that explicitly selected file.
An explicitly selected missing file fails before discovery. `run` loads the
selected configuration before static analysis or AWS client creation.

Examples below are sections to edit in the chosen configuration. Preserve
its other settings; do not add duplicate YAML keys. The old `diagrams:`
section in the sample configuration is historical and is not read by the CLI.

## Hide connections while retaining their resources

To hide DynamoDB Stream connections to Lambda:

```yaml
edge_types:
  include: []
  exclude:
    - dynamodb_stream
```

To also hide EventBridge Rule and Scheduler connections:

```yaml
edge_types:
  include: []
  exclude:
    - dynamodb_stream
    - eventbridge_trigger
    - scheduler_trigger
```

The DynamoDB tables, Lambda functions, Rules and Schedules remain as nodes.
They may become disconnected, and D2 recalculates their layout. To hide a node
as well, use a resource exclusion below. Removing an exclusion restores an
eligible connection already present in the saved graph; it does not discover
or invent connections.

**The current Mandate diagram's `EventBridge Triggers` box contains a
Scheduler Schedule. Its connection type is `scheduler_trigger`, not
`eventbridge_trigger`.** Box titles are not configuration identifiers.

| Connection type | Meaning |
|---|---|
| `dynamodb_stream` | DynamoDB stream → Lambda |
| `kinesis_trigger` | Kinesis stream → Lambda |
| `sqs_trigger` | SQS → Lambda event source mapping |
| `sqs_consumer` | Queue → consumer; often inferred from IAM permissions |
| `sqs_producer` | Producer → queue; often inferred from IAM permissions |
| `storage_access` | Compute → storage, including DynamoDB and S3 |
| `apigw_integration` | API Gateway → compute; inspect the recorded evidence |
| `eventbridge_trigger` | EventBridge Rule → Lambda, ECS or SQS |
| `scheduler_trigger` | Scheduler Schedule → Lambda from supported local CFN enrichment |
| `sns_subscription` | SNS → subscription endpoint |
| `http_call` | HTTP dependency inferred from configuration |
| `sqs_produce`, `sqs_consume` | Separate legacy names emitted by static repository analysis |
| `event_trigger` | Generic relationship from a resource with explicit endpoint metadata |

Names are matched exactly against `edges[].type` in the saved graph. The
filter accepts strings without validating them against an enum: a typo can
silently match nothing. The graph and the `.view.json` report are the reference
for the actual types and filter results.

When `include` is nonempty, only those types are eligible and `exclude` is
ignored. When both are empty, no connection-type filtering is applied.
The filter affects the rendered view, not the inventory or saved graph.
The report records filtered relationships as `excluded_edge_type`.

**Scope:** `edge_types` applies to every component generated with that file.
`--component pn-mandate` limits a particular generation command to Mandate.
For component-scoped exclusions use `render.view.edge_exclusions`:

```yaml
render:
  view:
    edge_exclusions:
      - component: pn-mandate
        edge_type: dynamodb_stream
      - component: pn-mandate
        edge_type: scheduler_trigger
        source: pn-CscaMasterlistSyncSchedule
        target: pn-CscaMasterlistSync
```

`component` and `edge_type` are required exact strings. `source` and `target`
are optional exact node names from the saved graph (case-sensitive; no glob or
prefix matching). Fields within a rule are ANDed; rules are ORed. Omit both
endpoints to hide every connection of that type in that component. Types are
the same as the table above; unknown types match nothing. Duplicate rules are
deduplicated and sorted canonically. Inventory and graph remain unchanged.
Global `edge_types` still applies: these rules only exclude further, never
restore a globally hidden edge. Reports use `excluded_edge_rule` and include
`matched_rule`. Excluded/missing endpoints and global filters take precedence
in reporting. Resource `exclusions` below remain independent and unchanged.

## Hide resource nodes in the view

```yaml
render:
  view:
    exclusions:
      - component: pn-delivery
        resource_type: lambda
        name_prefix: pn-delivery-versioning-
      - component: example-service
        resource_type: aws_scheduler_schedule
        name_prefix: example-nightly-
```

Each rule requires all three nonempty fields. Component and type match
exactly; `name_prefix` is a literal, case-sensitive prefix, not a glob or a
regular expression. A complete name is still treated as a prefix, so longer
names starting with it also match. An empty prefix is rejected.

The node and its incident connections are hidden only from the view. Inventory
and graph stay intact. Removing the rule restores the node if it is supported
by the selected template. `.view.json` records `excluded` resources and
`excluded_endpoint` connections. There is no resource include-only list or
include rule that overrides an exclusion.

Use the resource type stored in the inventory/graph, not its AWS display name:

| Family | Known normalized node types |
|---|---|
| Compute | `ecs_service`, `lambda`, `step_function`, `glue_job`, `batch_job` |
| Storage | `dynamodb`, `s3`, `elasticache`, `rds`, `opensearch`, `efs` |
| Messaging | `sqs`, `sns`, `kinesis`, `eventbridge_rule`, `eventbridge_bus` |
| Scheduler | `aws_scheduler_schedule` |
| Ingress/network | `apigw`, `apigw_v2`, `alb`, `nlb`, `target_group`, `cloudfront` |
| Security | `aws_wafv2_webacl`, `cognito_user_pool` |

This is the model's registered vocabulary, not a guarantee that every discovery
source normalizes every AWS service to these names or every template renders
all of them. For example, CFN discovery also exposes `eventbridge_pipe` and
falls back to normalized AWS type strings for unknown types. A filter can
match such a type if it exists in the graph, but that does not add renderer
support. ECS templates omit unsupported isolated clusters and reject connected
unsupported clusters; generic/Lambda templates have different coverage.

## Filter the resolved graph

These settings change what reaches the graph, unlike the view filters above:

```yaml
resource_kinds:
  skip:
    - aws_wafv2_webacl
    - target_group
```

`skip` applies globally to the listed resource types. Remove the override to
restore the built-in classification. `resource_kinds.node` can reclassify a
type as a node, but cannot create missing inventory resources or guarantee
layout/icon support. `resource_kinds.edge` requires usable endpoint metadata;
it does not infer arbitrary relationships.

The built-in registry skips `aws_lambda_layerversion` and
`aws_cloudformation_stack`; it treats `aws_lambda_eventsourcemapping` as a
relationship resource. The shipped YAML additionally skips WAF and target groups.

`exclude.name_patterns` filters names during resolve using case-insensitive
glob matching. Supplying this list replaces the resolver's default name
patterns, including its DLQ patterns, so preserve those patterns if needed.

| CLI option on `resolve` / `run` | Effect |
|---|---|
| `--include-dlq` | Removes DLQ-related name-pattern exclusions; other exclusions still apply |
| `--exclude-external-queues` | Disables insertion of SQS nodes inferred from IAM references outside the component inventory |

The latter is not a blanket removal of every externally owned queue already
present in an inventory. `--include-dlq` does not override resource-type or
view exclusions. Graph-filter changes require resolving again from the saved
inventory; regenerating an old graph alone cannot restore removed nodes.

`discover --skip-types` filters CFN resource types when exporting the inventory.
It is a separate discovery-stage option (not exposed on `run`); changing it
may require another AWS discovery and therefore a separately authorized command.

## Regenerate locally, without AWS

After changing only `edge_types` or `render.view`, use the saved graph:

```bash
pn-docgen generate \
  --graph /path/to/saved_graph.json \
  --config /path/to/central/pndocgen.yaml \
  --component pn-mandate \
  --output-dir /path/to/new-output-directory \
  --level l3 --detail-level detailed --theme white \
  --render --render-format svg
```

Use a fresh output directory to preserve previous diagrams. Inspect the
`.view.json` companion for the reasons resources/connections were included,
filtered, aggregated or unsupported. Regeneration reads local files and
invokes D2 locally; AWS access belongs to discovery, not this command.
## PNG from the normalized SVG

Install the optional Rust renderer with `pip install '.[raster]'` from the
repository (or `pip install 'pn-docgen[raster]'` from a package index that
provides pn-docgen). The pinned dependency is `resvg-py==0.5.0`, using resvg
0.48.1. SVG-only use does not require this dependency.

Use `--render --render-format png` with `generate` or `run`. D2 renders SVG
first, then all normalizations and attribution are applied. resvg converts
that exact final SVG to PNG. Both files are saved; there is no direct D2-to-PNG
path. Fonts are extracted from D2's embedded WOFF subsets into temporary files;
no system fonts or network downloads are used during rendering.

PNG dimensions follow the SVG canvas at one pixel per SVG unit. PNG metadata
contains the renderer versions, the source SVG SHA-256 and icon attribution.
The SVG itself is unchanged by rasterization. If the optional dependency is
missing, a font cannot be verified, or the canvas exceeds 40 megapixels, PNG
generation fails explicitly. A failed conversion does not publish a partial
PNG or a new SVG; pre-existing artifacts are preserved. An existing sibling
SVG is accepted only if byte-identical to the new canonical render.

# Central classification and essential view (candidate 051)

Resource visibility uses the shared resource registry for both ECS and Lambda,
including replay of saved graphs. API Models and RequestValidators are internal
details and excluded by default. `resource_kinds` overrides remain supported.
View reports record `excluded_resource_kind`. The essential view additionally
omits nodes assigned to `security` or `misc` by default, recording
`excluded_auxiliary_cluster`. This central policy applies to every component,
both detail levels and both ECS and generic Lambda templates. Unknown resources
classified as `misc` are therefore omitted. Source graphs remain unchanged;
incident edges are recorded as `excluded_endpoint`. Missing icons on retained
nodes still use rectangles, not invalid images.

Configure the excluded groups centrally:

```yaml
render:
  view:
    excluded_clusters: [security, misc]
```

The list replaces the default. Omit the key (or the configuration file) to
retain `[security, misc]`; use `[]` to disable cluster exclusions. Add exact
cluster names, for example `[security, misc, rules]`, to hide further groups.
Other resource-kind, resource-name and edge filters remain active: clearing
this list does not restore resources excluded by those filters. The report
keeps the `excluded_auxiliary_cluster` status for any excluded group.

Schedules are trigger nodes in `rules` and remain visible. Authorizers are
classified as `security` and omitted by the essential view. Neither placement
implies an edge. Exclusion is a presentation choice, not proof that a resource
has no runtime role. Pipes, Firehose streams and EventBridge buses are classified
as messaging; S3 Tables table buckets and tables are storage. Namespaces remain
support objects. Classification does not invent missing relationships.
When the specialized ECS template cannot represent a selected cluster, rendering
uses the generic layout and records `layout_fallback` instead of hiding nodes.

Future CFN captures save a private `.raw.json` sidecar before normalization.
It contains collected resource records and discovery issues, not every AWS API
response. Normalized inventories remain filtered. Keep raw files out of public
repositories; existing capture files are never overwritten.

## Resource purposes and dataflow evidence

S3 Tables is represented by one node per Table Bucket in both detail levels.
Tables and namespaces stay in the inventory, not as separate visible nodes.
Verified table_storage ownership projects table relationships to their bucket;
containment is recorded in the view report rather than drawn as a dataflow.
Missing or ambiguous ownership is reported as unresolved_table_bucket. Distinct
buckets are never merged. Explicit resource and edge exclusions remain active.

The essential view excludes `logging` and `test` purposes by default, independently
of detail level. Application resources remain visible. A Firehose backup bucket
is assigned `logging` only from its described backup relationship, not its name.
A bucket also used as a primary delivery destination remains application storage.
Tracing/test roles can be declared centrally without per-component exceptions:

```yaml
render:
  view:
    excluded_purposes: [logging, test]
    purpose_rules:
      - resource_type: aws_kinesisfirehose_deliverystream
        name_prefix: tracing-
        purpose: tracing
```

The supported purposes are `application`, `tracing`, `logging`, and `test`.
Rules match the normalized resource type and name prefix, apply to every component,
and override inferred purpose. Conflicting matches fail explicitly. Names in the
example are illustrative, not built-in conventions. `excluded_purposes: []` enables
operational details; `[logging, test, tracing]` also hides explicitly classified
tracing resources. Other filters remain active. Reports record `excluded_purpose`
and incident `excluded_endpoint` edges; the source graph is unchanged.

Live enrichment uses read-only `pipes:DescribePipe` and
`firehose:DescribeDeliveryStream` for resources already discovered through CFN,
alongside existing EventBridge `ListTargetsByRule`. Supported typed targets include
Kinesis, EventBridge buses, Firehose, SNS and Step Functions, in addition to the
existing ECS/Lambda/SQS targets. Missing permissions, unsupported destinations and
endpoints outside the captured component are discovery issues, not invented nodes.
The raw sidecar additionally preserves dataflow descriptions, links and identity
scopes. Older captures lack this evidence; replay cannot recover it retroactively.

# Text-aware container fitting

`render.normalization.separate_node_labels: true` enables bounded clearance of
bottom resource labels after the other passes. It first tries moving an unattached
icon and label together horizontally within the container (`max_node_shift`),
then moving only the label downward (`max_label_shift`). Attached routes never
move in this pass. Candidates must clear the affected label without introducing
collisions, overflow or broken attachments; unsupported geometry abstains. Safe
bundle centering is retried after a successful clearance. Set the flag to `false`
to disable the pass. This is a guarded local correction, not a universal layout
guarantee; an existing collision may remain when no safe candidate exists.

`separate_container_titles: true` enables a final title-only pass. If a
container title intersects a route or another measured object, the pass
searches the closest safe horizontal position inside its existing boundary.
`max_title_shift: 120` bounds the displacement; `text_container_margin` is
the minimum horizontal inset. It changes only the title's x coordinate:
icons, resource labels, container bounds, paths and endpoints stay fixed.
A clear title is never moved. The result is deterministic and idempotent;
unsupported text/paths or no safe interval cause a reported abstention in
`container_titles`. A corrected title can be off-center within its container.
Set `separate_container_titles: false` to disable only this pass.

`align_node_labels: true` and `node_label_gap: 4` normalize the measured gap
between an image bottom and its plain bottom label, including ragged grids.
Only the label moves; collisions/overflow cause refusal. These settings are
independent of `fit_text_containers`. The gap is measured to a conservative
font box, not the text baseline, with the normalizer's 0.6px tolerance.

The SVG pipeline measures supported plain text using the embedded D2 WOFF
fonts (fontTools 4.60.1, no browser or font network requests at runtime).
Under `render.normalization`, `fit_text_containers: true` enables horizontal
fitting of explicit leaf columns, `text_container_margin: 16` controls padding,
and `max_container_growth: 240` bounds width/ancestor expansion in SVG units.
Setting `fit_text_containers: false` disables this pass without disabling
other normalization. Negative/non-finite limits and non-boolean flags fail.

The pass changes borders/titles and, if necessary, ancestor/canvas bounds;
it does not rename nodes, truncate text, move node icons or reroute arrows.
Attached container endpoints, unsafe collisions and unsupported text/geometry
cause a reported abstention. This is not a guarantee that every label in any
possible topology will fit. Inspect `text_containers` in the render report/log.
For overfull routing envelopes, `port_capacity_classes: [ecs_hero]` allows
bounded square-image resizing and centering, with `max_port_icon_growth: 24`
and `port_icon_padding: 2`. `max_node_shift` still applies. Use an empty list
to disable this fallback. It changes SVG placement/size, not icon artwork.
Ordinary `aws_node` icons stay uniformly sized unless explicitly opted in.
Only compatible terminal axes, agreeing bundle centers and safe local changes
are accepted; paths are not rerouted and every other attachment is checked.
