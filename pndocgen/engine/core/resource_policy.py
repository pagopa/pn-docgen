"""Shared CFN support-type policy; no component-specific exceptions."""
CFN_SUPPORT_TYPES: frozenset = frozenset({
    "AWS::CloudWatch::Alarm",
    "AWS::Logs::LogGroup",
    "AWS::Logs::MetricFilter",
    "AWS::Logs::SubscriptionFilter",
    "AWS::KMS::Key",
    "AWS::KMS::Alias",
    "AWS::IAM::Role",
    "AWS::IAM::Policy",
    "AWS::IAM::ManagedPolicy",
    "AWS::Lambda::Permission",
    "AWS::Lambda::Version",
    "AWS::Lambda::Alias",
    "AWS::Lambda::EventInvokeConfig",
    "AWS::ApplicationAutoScaling::ScalableTarget",
    "AWS::ApplicationAutoScaling::ScalingPolicy",
    "AWS::EC2::SecurityGroup",
    "AWS::ECS::TaskDefinition",
    "AWS::ApiGateway::Deployment",
    "AWS::ApiGateway::Stage",
    # REST routes/methods belong to the API node, not standalone L3 nodes.
    # Different methods may have physical IDs ending in the same verb (GET).
    "AWS::ApiGateway::Method",
    "AWS::ApiGateway::Resource",
    "AWS::ApiGateway::BasePathMapping",
    "AWS::WAFv2::LoggingConfiguration",
    "AWS::WAFv2::WebACLAssociation",
    "AWS::WAFv2::IPSet",
    "AWS::SQS::QueuePolicy",
    # A bucket policy is not a separate architecture node. CloudFormation may
    # identify it by the bucket name, which would collide with the bucket node.
    "AWS::S3::BucketPolicy",
    "AWS::ElasticLoadBalancingV2::ListenerRule",
    "AWS::EFS::AccessPoint",
    "AWS::Glue::Table",
    "AWS::CloudWatch::Dashboard",
})
