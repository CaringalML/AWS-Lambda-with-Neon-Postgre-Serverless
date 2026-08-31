"""Read-only audit: prove nothing from the NovaDrive stack is still alive.

Every call is a list/describe. A probe that raises is reported as ERROR rather
than counted as "clean" — a permissions failure must never read as an all-clear.
"""
import os
import boto3

PREFIX = "serverless-web-app"
BUCKET = "nova-drive-caringal"
REGION = "us-east-1"

rows = []
found = 0


def client(service, region=REGION):
    return boto3.client(service, region_name=region)


def probe(label, fn):
    global found
    try:
        hits = [h for h in (fn() or []) if h]
    except Exception as exc:
        rows.append((label, "ERROR", type(exc).__name__))
        return
    if hits:
        found += len(hits)
        rows.append((label, str(len(hits)), ", ".join(map(str, hits[:3]))))
    else:
        rows.append((label, "0", "none"))


def pages(service, op, region=REGION, **kw):
    return client(service, region).get_paginator(op).paginate(**kw)


probe("S3 drive bucket", lambda: [
    b["Name"] for b in client("s3").list_buckets()["Buckets"] if b["Name"] == BUCKET])

probe("Lambda functions", lambda: [
    f["FunctionName"] for p in pages("lambda", "list_functions")
    for f in p["Functions"] if f["FunctionName"].startswith(PREFIX)])

probe("DynamoDB tables", lambda: [
    t for p in pages("dynamodb", "list_tables")
    for t in p["TableNames"] if t.startswith(PREFIX)])

probe("CloudFront distributions", lambda: [
    d["Id"] for p in pages("cloudfront", "list_distributions")
    for d in (p["DistributionList"].get("Items") or [])
    if "nova-drive" in str(d.get("Origins", {}))])

probe("API Gateway v2 APIs", lambda: [
    a["ApiId"] for a in client("apigatewayv2").get_apis()["Items"]
    if a["Name"].startswith(PREFIX)])

probe("Cognito user pools", lambda: [
    u["Id"] for u in client("cognito-idp").list_user_pools(MaxResults=60)["UserPools"]
    if u["Name"].startswith(PREFIX)])

probe("Batch job queues", lambda: [
    q["jobQueueName"] for q in client("batch").describe_job_queues()["jobQueues"]
    if q["jobQueueName"].startswith(PREFIX)])

probe("Batch compute envs", lambda: [
    e["computeEnvironmentName"]
    for e in client("batch").describe_compute_environments()["computeEnvironments"]
    if e["computeEnvironmentName"].startswith(PREFIX)])

probe("Batch job definitions", lambda: [
    d["jobDefinitionName"]
    for d in client("batch").describe_job_definitions(status="ACTIVE")["jobDefinitions"]
    if d["jobDefinitionName"].startswith(PREFIX)])

probe("SNS topics", lambda: [
    t["TopicArn"] for p in pages("sns", "list_topics")
    for t in p["Topics"] if PREFIX in t["TopicArn"]])

probe("ACM certificates", lambda: [
    a["CertificateArn"] for p in pages("acm", "list_certificates")
    for a in p["CertificateSummaryList"]
    if "nodepulsecaringal" in a.get("DomainName", "")])

probe("CloudWatch log groups", lambda: [
    g["logGroupName"] for p in pages("logs", "describe_log_groups")
    for g in p["logGroups"] if PREFIX in g["logGroupName"]])

probe("IAM roles", lambda: [
    r["RoleName"] for p in pages("iam", "list_roles")
    for r in p["Roles"] if r["RoleName"].startswith(PREFIX)])

probe("SSM parameters", lambda: [
    s["Name"] for p in pages("ssm", "describe_parameters")
    for s in p["Parameters"]
    if PREFIX in s["Name"] or "novadrive" in s["Name"].lower()])

probe("Security groups", lambda: [
    g["GroupId"] for g in client("ec2").describe_security_groups()["SecurityGroups"]
    if g["GroupName"].startswith(PREFIX)])

probe("Tagged Environment=dev", lambda: [
    r["ResourceARN"] for p in pages(
        "resourcegroupstaggingapi", "get_resources",
        TagFilters=[{"Key": "Environment", "Values": ["dev"]}])
    for r in p["ResourceTagMappingList"]])

out = ["## Live resource sweep (us-east-1)", "",
       "| resource | count | detail |", "|---|---|---|"]
for label, count, detail in rows:
    flag = "" if count == "0" else " **"
    out.append(f"| {label} | {count}{flag} | {detail[:90]} |")

# Batch keeps deregistered job definitions visible as INACTIVE revisions.
# They are metadata and cost nothing, so separate them from live resources.
out += ["", "## Batch job definitions by status", ""]
for status in ("ACTIVE", "INACTIVE"):
    try:
        defs = [d["jobDefinitionArn"] for d in client("batch").describe_job_definitions(
            status=status)["jobDefinitions"]
            if d["jobDefinitionName"].startswith(PREFIX)]
        out.append(f"- {status}: {len(defs)}" + (f" — {defs[0].split('/')[-1]}" if defs else ""))
    except Exception as exc:
        out.append(f"- {status}: error {type(exc).__name__}")

# The stack could have been applied elsewhere at some point in its life.
# List the ARNs, not just a count — "17 tagged" means nothing on its own.
out += ["", "## Other regions", "", "| region | tagged | lambdas |", "|---|---|---|"]
elsewhere = {}
for region in ("ap-southeast-2", "ap-southeast-1", "us-west-2", "eu-west-1"):
    try:
        arns = [r["ResourceARN"]
                for p in pages("resourcegroupstaggingapi", "get_resources", region,
                               TagFilters=[{"Key": "Environment", "Values": ["dev"]}])
                for r in p["ResourceTagMappingList"]]
        lambdas = len([f for p in pages("lambda", "list_functions", region)
                       for f in p["Functions"]
                       if f["FunctionName"].startswith(PREFIX)])
        if arns:
            elsewhere[region] = arns
        out.append(f"| {region} | {len(arns)} | {lambdas} |")
        found += lambdas
    except Exception as exc:
        out.append(f"| {region} | error: {type(exc).__name__} | - |")

for region, arns in elsewhere.items():
    out += ["", f"### Every Environment=dev ARN in {region}", "", "```"]
    out += arns[:40]
    if len(arns) > 40:
        out.append(f"... and {len(arns) - 40} more")
    out.append("```")

out += ["", "## Left standing on purpose (never Terraform-managed)", ""]
try:
    client("s3").head_bucket(Bucket="nova-drive-terraform-state")
    objects = sum(p.get("KeyCount", 0) for p in
                  pages("s3", "list_objects_v2", Bucket="nova-drive-terraform-state"))
    out.append(f"- state bucket `nova-drive-terraform-state` — exists, {objects} object(s)")
except Exception:
    out.append("- state bucket `nova-drive-terraform-state` — gone")
try:
    client("dynamodb").describe_table(TableName="novadrive-terraform-lock")
    out.append("- lock table `novadrive-terraform-lock` — exists")
except Exception:
    out.append("- lock table `novadrive-terraform-lock` — gone")

errors = [r for r in rows if r[1] == "ERROR"]
out += ["", f"### Sweep total: {found} stack resource(s), {len(errors)} probe error(s)"]

report = "\n".join(out)
print(report)
with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
    fh.write(report + "\n")
with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as fh:
    fh.write(f"sweep_found={found}\nprobe_errors={len(errors)}\n")
