from pathlib import Path

from pndocgen.engine.core.config import (
    AppConfig,
    DiscoveryConfig,
    EcsDependencyConfig,
    ProjectConfig,
)
from pndocgen.sources.cfn.cfn_analyzer import CfnAnalyzer


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_cfn_analyzer_legacy_mode_uses_storage_and_microservice(tmp_path: Path) -> None:
    repo = tmp_path / "pn-example"
    _write(
        repo / "scripts/aws/cfn/storage.yml",
        """
Resources:
  OrdersQueue:
    Type: AWS::SQS::Queue
    Properties:
      QueueName: !Sub '${ProjectName}-orders'
  DataBucket:
    Type: AWS::S3::Bucket
    Properties:
      BucketName: !Sub '${ProjectName}-data'
""".strip(),
    )
    _write(
        repo / "scripts/aws/cfn/microservice.yml",
        """
Resources:
  WorkerFunction:
    Type: AWS::Lambda::Function
    Properties:
      FunctionName: !Sub '${ProjectName}-worker'
  WorkerPolicy:
    Type: AWS::IAM::Policy
    Properties:
      PolicyName: worker-policy
      PolicyDocument:
        Statement:
          - Effect: Allow
            Action:
              - sqs:SendMessage
              - sqs:ReceiveMessage
            Resource:
              - OrdersQueueARN
ContainerEnvEntry1: 'PN_SVC_DELIVERYPUSHBASEURL=http://internal.local/path'
""".strip(),
    )

    cfg = AppConfig(
        project=ProjectConfig(prefix="pn-"),
        discovery=DiscoveryConfig(
            cfn_paths={
                "storage": "scripts/aws/cfn/storage.yml",
                "microservice": "scripts/aws/cfn/microservice.yml",
            },
            cfn_analyzer=DiscoveryConfig.CfnAnalyzerConfig(mode="legacy"),
            ecs_dependencies=EcsDependencyConfig(
                internal_domains=["internal.local"],
                service_alias_map={"deliverypush": "pn-delivery-push"},
            ),
        ),
    )

    analysis = CfnAnalyzer(repo_path=repo, app_config=cfg).analyze()

    assert "pn-example-orders" in analysis.owned_queues
    assert "pn-example-data" in analysis.s3_buckets
    assert "pn-example-worker" in analysis.lambdas
    assert "<OrdersQueue>" in analysis.producer_queues
    assert "<OrdersQueue>" in analysis.consumer_queues
    assert "pn-delivery-push" in analysis.http_clients


def test_cfn_analyzer_generic_mode_supports_single_template_repo(tmp_path: Path) -> None:
    repo = tmp_path / "pn-generic"
    _write(
        repo / "infra/template.yml",
        """
Resources:
  OrdersQueue:
    Type: AWS::SQS::Queue
    Properties:
      QueueName: !Sub '${ProjectName}-orders'
  WorkerFunction:
    Type: AWS::Lambda::Function
    Properties:
      FunctionName: !Sub '${ProjectName}-worker'
  WorkerPolicy:
    Type: AWS::IAM::Policy
    Properties:
      PolicyName: worker-policy
      PolicyDocument:
        Statement:
          - Effect: Allow
            Action:
              - sqs:SendMessage
              - sqs:ReceiveMessage
            Resource:
              - Ref: OrdersQueue
ContainerEnvEntry1: 'PN_SVC_DELIVERYPUSHBASEURL=http://internal.local/path'
""".strip(),
    )

    cfg = AppConfig(
        project=ProjectConfig(prefix="pn-"),
        discovery=DiscoveryConfig(
            cfn_analyzer=DiscoveryConfig.CfnAnalyzerConfig(
                mode="generic",
                template_files=["infra/template.yml"],
            ),
            ecs_dependencies=EcsDependencyConfig(
                internal_domains=["internal.local"],
                service_alias_map={"deliverypush": "pn-delivery-push"},
            ),
        ),
    )

    analysis = CfnAnalyzer(repo_path=repo, app_config=cfg).analyze()

    assert "pn-generic-orders" in analysis.owned_queues
    assert "pn-generic-worker" in analysis.lambdas
    assert "<OrdersQueue>" in analysis.producer_queues
    assert "<OrdersQueue>" in analysis.consumer_queues
    assert "pn-delivery-push" in analysis.http_clients

