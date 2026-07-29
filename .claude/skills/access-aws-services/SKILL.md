---
name: access-aws-services
forged: true
forged_by: alpha
forged_date: "2026-03-28"
description: "Runs authenticated AWS CLI and boto3 operations against AyoAI cloud resources: DynamoDB queries, CloudWatch metrics and logs, S3 objects, Lambda invocations, EC2 inspection and SSM send-command. Use whenever the agent needs to inspect or modify AWS infrastructure — querying tables, reading logs, listing buckets, invoking Lambdas, checking EC2 or Parameter Store. Credentials come from .env.local via env-read.sh; MUST use world/scripts/aws-exec.sh — never raw aws CLI with hardcoded keys."
user-invocable: false
triggers: [aws, ec2, lambda, dynamodb, cloudwatch, s3, ssm, boto3, aws-cli, aws-exec, describe-instances, send-command, parameter-store]
tools_used: [Bash]
companion_scripts: [world/scripts/aws-exec.sh, world/scripts/aws-lambda-invoke.sh]
conventions: [secrets, infrastructure]
minimum_mode: autonomous
revision_id: "skill-bootstrap-access-aws-services-1e9d4f"
previous_revision_id: null
---

# /access-aws-services — AWS Service Access

Access AWS services (DynamoDB, CloudWatch, S3, Lambda, EC2) via authenticated AWS CLI.

## Companion Scripts

- `world/scripts/aws-exec.sh <aws-cli-args...>` — Run any AWS CLI command with credentials
- `world/scripts/aws-lambda-invoke.sh <function-name> '<json>'` — Invoke Lambda, return response

## Common Patterns

### DynamoDB
```bash
# Scan a table
Bash: world/scripts/aws-exec.sh dynamodb scan --table-name WorldBuilders --max-items 5 --region us-east-2

# Query with key condition
Bash: world/scripts/aws-exec.sh dynamodb query --table-name AyoaiAdminState --key-condition-expression "PK = :pk" --expression-attribute-values '{":pk":{"S":"heartbeat"}}' --region us-east-2
```

Tables: WorldBuilders, AyoaiAdminState, AyoaiAdminAuditTrail, AyoaiWarmPool, AyoaiWebAnalytics

### Lambda
```bash
# Invoke synchronously (get response)
Bash: world/scripts/aws-lambda-invoke.sh GetListOfServers '{"callerAccountId":"..."}'

# Fire-and-forget (async)
Bash: world/scripts/aws-exec.sh lambda invoke --function-name SendInfoAlert --invocation-type Event --payload '{"InfoMessage":"test"}' --cli-binary-format raw-in-base64-out /dev/null --region us-east-2
```

### EC2
```bash
# List running instances
Bash: world/scripts/aws-exec.sh ec2 describe-instances --filters "Name=instance-state-name,Values=running" --query "Reservations[*].Instances[*].[InstanceId,PublicIpAddress,Tags[?Key=='Name'].Value|[0]]" --output table --region us-east-2
```

### S3
```bash
# List objects
Bash: world/scripts/aws-exec.sh s3api list-objects-v2 --bucket my-bucket --prefix "some/prefix/" --max-items 10 --region us-east-2

# Download
Bash: world/scripts/aws-exec.sh s3 cp s3://bucket/key ./local-file --region us-east-2
```

### CloudWatch
```bash
# Query log groups
Bash: world/scripts/aws-exec.sh logs describe-log-groups --region us-east-2

# Get recent log events
Bash: world/scripts/aws-exec.sh logs get-log-events --log-group-name /aws/lambda/FunctionName --log-stream-name "stream" --limit 20 --region us-east-2
```

## Security

- Credentials: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION` from `.env.local`
- Region: `us-east-2` (Ohio)
- Account: 891377285145

## Infra-Health Component

Component: `aws-api`. Probe: run `aws-exec.sh sts get-caller-identity --region us-east-2`.

## Return Protocol

See `.claude/rules/return-protocol.md` — last action must be a tool call, not text.
The last `aws-exec.sh` invocation is itself the terminal tool call. Never end with a
text summary of the AWS response — let the script output stand as the final action.
