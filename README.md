# pn-docgen

`pn-docgen` is an AWS architecture diagram generator.

It discovers infrastructure and runtime relationships, resolves resources into
microservice components, and generates D2 diagrams (L3 component detail),
optionally rendered as PNG or SVG.

## How it works

```text
AWS APIs (CloudFormation, Lambda, ECS, ...)  [read-only]
              ↓
pn-docgen discover  →  inventory.json
              ↓
pn-docgen resolve   →  component_graph.json
              ↓
pn-docgen generate  →  *.d2 (+ *.png with --render)
```

## Quick start

### 1) Install

```bash
pip install -e pn-docgen/
```

### 2) Download AWS icons (one time)

```bash
python pn-docgen/scripts/download_aws_icons.py
```

### 3) Install D2 CLI (for PNG rendering)

```bash
# macOS
brew install d2
```

Other platforms: https://d2lang.com/tour/install

### 4) Configure AWS credentials

```bash
aws sso login --profile <your-profile>
```

---

## Commands overview

| Command | Purpose | Typical output |
|---|---|---|
| `discover` | Read AWS resources + inferred relationships | `inventory.json` |
| `resolve` | Group resources by component and normalize edges | `component_graph.json` |
| `generate` | Generate L3 diagrams from a graph | `.d2` (+ `.png` / `.svg` with `--render`) |
| `run` | Full pipeline (`discover` → `resolve` → `generate`) | inventory + graph + L3 diagrams |

---

## Example workflows

### A) Discovery only

Single component:

```bash
python -m pndocgen.cli discover \
  --profile <your-profile> \
  --region <aws-region> \
  --discovery-source cfn \
  --component <component-name> \
  --output inventory.json
```

All components:

```bash
python -m pndocgen.cli discover \
  --profile <your-profile> \
  --region <aws-region> \
  --discovery-source cfn \
  --output inventory.json
```

### B) Resolve only

```bash
python -m pndocgen.cli resolve \
  --inventory inventory.json \
  --include-dlq \
  --exclude-external-queues \
  --output component_graph.json
```

### C) Generate only (from existing graph)

L3 simplified:

```bash
python -m pndocgen.cli generate \
  --graph component_graph.json \
  --output-dir docs/generated \
  --level l3 \
  --detail-level simplified \
  --pattern ecs_microservice \
  --render-format png \
  --render
```

L3 detailed:

```bash
python -m pndocgen.cli generate \
  --graph component_graph.json \
  --output-dir docs/generated \
  --level l3 \
  --detail-level detailed \
  --pattern ecs_microservice \
  --render-format png \
  --render
```

### D) Full pipeline in one command

Single component, detailed L3:

```bash
python -m pndocgen.cli run \
  --profile <your-profile> \
  --region <aws-region> \
  --discovery-source cfn \
  --component <component-name> \
  --include-dlq \
  --exclude-external-queues \
  --level l3 \
  --detail-level detailed \
  --pattern ecs_microservice \
  --output-dir docs/generated \
  --render-format png \
  --render
```

All components, L3:

```bash
python -m pndocgen.cli run \
  --profile <your-profile> \
  --region <aws-region> \
  --discovery-source cfn \
  --level l3 \
  --detail-level simplified \
  --pattern auto \
  --output-dir docs/generated \
  --render-format svg \
  --render
```

---

## Key options

- `--discovery-source`: `tag` | `cfn` | `both`
- `--level`: `l3`
- `--detail-level`: `simplified` | `detailed`
- `--pattern`: `auto` | `ecs_microservice` | `lambda_microservice`
- `--include-dlq`: include DLQ queues in resolved graph (default: excluded)
- `--exclude-external-queues`: exclude SQS queues referenced by IAM policies but not discovered in component stacks (default: included)
- `--render`: render image via D2 CLI
- `--render-format`: `png` | `svg` (default: `png`, used with `--render`)

---

## Output structure

Generated files are grouped by component under the selected output directory.

Typical artifacts:
- `<component>_<timestamp>_inventory.json`
- `<component>_<timestamp>_graph.json`
- `<component>_<timestamp>_L3.d2` or `<component>_<timestamp>_L3_detailed.d2`

---

## Configuration (`pndocgen.yaml`)

You can customize classification and filtering rules with a local config file:

```yaml
resource_kinds:
  skip:
    - aws_wafv2_webacl
    - target_group
  node:
    - aws_lambda_layerversion

exclude:
  name_patterns:
    - "*-dlq"
    - "*-dead-letter*"
```

---

## Requirements

- Python 3.11+
- AWS credentials (SSO profile or `AWS_*` env vars)
- D2 CLI (`d2`) for PNG rendering
- Read-only AWS permissions:
  - `cloudformation:ListStackResources`
  - `resourcegroupstaggingapi:GetResources`
  - `lambda:ListEventSourceMappings`
  - `ecs:DescribeServices`
  - `ecs:DescribeTaskDefinition`
