"""Delete the orphaned v2 stack in ap-southeast-2.

Its Terraform state bucket and lock table are both gone, so Terraform can no
longer manage these — they have to be removed by direct API call.

Everything is driven by an explicit allow-list of exact identifiers. There is
no wildcard or prefix sweep, because the Environment=dev tag in this region is
also worn by unrelated projects (newsarchive alarms, a CloudStruct ECS task
definition) that must survive untouched.

Run with MODE=plan (default) to list what would happen; MODE=apply to do it.
"""
import os
import time
import boto3

REGION = "ap-southeast-2"
MODE = os.environ.get("MODE", "plan")
APPLY = MODE == "apply"

# Exact identifiers only — never a pattern.
BATCH_QUEUE = "serverless-web-app-dev"
BATCH_COMPUTE = "serverless-web-app-dev"
BATCH_JOBDEF_PREFIX = "serverless-web-app-zip-folder-dev"
APIGW_API_ID = "qe8x6zlg14"
APIGW_DOMAIN = "drive.nodepulsecaringal.xyz"
ACM_CERT = ("arn:aws:acm:ap-southeast-2:939737198590:certificate/"
            "8452be32-bf92-4016-a1f8-5c966d61823e")
SECURITY_GROUP = "sg-08501562b24d931d6"
LOG_GROUPS = ["/aws/batch/serverless-web-app-dev",
              "/aws/lambda/serverless-web-app-dev"]
SNS_TOPIC = "arn:aws:sns:ap-southeast-2:939737198590:serverless-web-app-alerts-dev"
SSM_PARAMS = [
    "/serverless-web-app/dev/resend-api-key",
    "/serverless-web-app/dev/admin-password",
    "/serverless-web-app/dev/database-url",
    "/serverless-web-app/dev/cloudfront-signing-key",
]

# Anything matching these belongs to another project and is never touched.
NEVER_TOUCH = ("newsarchive", "CloudStruct")

log = []


def record(action, target, result):
    line = f"{'APPLY' if APPLY else 'PLAN '} | {action:<28} | {target:<58} | {result}"
    print(line)
    log.append(line)


def guard(target):
    for banned in NEVER_TOUCH:
        if banned.lower() in target.lower():
            raise SystemExit(f"REFUSING to touch {target!r} — matches {banned!r}")


def do(action, target, fn):
    """Run one deletion, or describe it in plan mode."""
    guard(target)
    if not APPLY:
        record(action, target, "would delete")
        return
    try:
        fn()
        record(action, target, "deleted")
    except Exception as exc:
        record(action, target, f"ERROR {type(exc).__name__}: {exc}"[:120])


c = lambda svc: boto3.client(svc, region_name=REGION)

# ── Batch: queue must be disabled and deleted before its compute environment ──
batch = c("batch")

queues = [q for q in batch.describe_job_queues()["jobQueues"]
          if q["jobQueueName"] == BATCH_QUEUE]
if queues:
    state = queues[0]["state"]
    if APPLY:
        if state != "DISABLED":
            batch.update_job_queue(jobQueue=BATCH_QUEUE, state="DISABLED")
            record("batch disable queue", BATCH_QUEUE, "disabled")
            for _ in range(30):
                time.sleep(5)
                q = batch.describe_job_queues(jobQueues=[BATCH_QUEUE])["jobQueues"]
                if not q or q[0]["status"] == "VALID":
                    break
        do("batch delete queue", BATCH_QUEUE,
           lambda: batch.delete_job_queue(jobQueue=BATCH_QUEUE))
        for _ in range(30):
            time.sleep(5)
            if not batch.describe_job_queues(jobQueues=[BATCH_QUEUE])["jobQueues"]:
                break
    else:
        record("batch delete queue", BATCH_QUEUE, f"would disable ({state}) then delete")
else:
    record("batch delete queue", BATCH_QUEUE, "already gone")

envs = [e for e in batch.describe_compute_environments()["computeEnvironments"]
        if e["computeEnvironmentName"] == BATCH_COMPUTE]
if envs:
    if APPLY:
        if envs[0]["state"] != "DISABLED":
            batch.update_compute_environment(computeEnvironment=BATCH_COMPUTE,
                                             state="DISABLED")
            record("batch disable compute env", BATCH_COMPUTE, "disabled")
            for _ in range(30):
                time.sleep(5)
                e = batch.describe_compute_environments(
                    computeEnvironments=[BATCH_COMPUTE])["computeEnvironments"]
                if not e or e[0]["status"] == "VALID":
                    break
        do("batch delete compute env", BATCH_COMPUTE,
           lambda: batch.delete_compute_environment(computeEnvironment=BATCH_COMPUTE))
    else:
        record("batch delete compute env", BATCH_COMPUTE,
               f"would disable ({envs[0]['state']}) then delete")
else:
    record("batch delete compute env", BATCH_COMPUTE, "already gone")

for status in ("ACTIVE", "INACTIVE"):
    for d in batch.describe_job_definitions(status=status)["jobDefinitions"]:
        if d["jobDefinitionName"].startswith(BATCH_JOBDEF_PREFIX) and status == "ACTIVE":
            arn = d["jobDefinitionArn"]
            do("batch deregister jobdef", arn.split("/")[-1],
               lambda a=arn: batch.deregister_job_definition(jobDefinition=a))

# ── API Gateway: the custom domain mapping holds the certificate ──────────────
for svc in ("apigatewayv2", "apigateway"):
    api = c(svc)
    try:
        if svc == "apigatewayv2":
            names = [d["DomainName"] for d in api.get_domain_names()["Items"]]
            if APIGW_DOMAIN in names:
                do("apigw delete domain", APIGW_DOMAIN,
                   lambda: api.delete_domain_name(DomainName=APIGW_DOMAIN))
            ids = [a["ApiId"] for a in api.get_apis()["Items"]]
            if APIGW_API_ID in ids:
                do("apigw delete api", APIGW_API_ID,
                   lambda: api.delete_api(ApiId=APIGW_API_ID))
        else:
            names = [d["domainName"] for d in api.get_domain_names()["items"]]
            if APIGW_DOMAIN in names:
                do("apigw(v1) delete domain", APIGW_DOMAIN,
                   lambda: api.delete_domain_name(domainName=APIGW_DOMAIN))
    except Exception as exc:
        record("apigw", svc, f"skip ({type(exc).__name__})")

# ── The rest ─────────────────────────────────────────────────────────────────
acm = c("acm")
do("acm delete certificate", ACM_CERT.split("/")[-1],
   lambda: acm.delete_certificate(CertificateArn=ACM_CERT))

logs = c("logs")
for group in LOG_GROUPS:
    do("logs delete group", group,
       lambda g=group: logs.delete_log_group(logGroupName=g))

sns = c("sns")
do("sns delete topic", SNS_TOPIC.split(":")[-1],
   lambda: sns.delete_topic(TopicArn=SNS_TOPIC))

ssm = c("ssm")
for param in SSM_PARAMS:
    do("ssm delete parameter", param,
       lambda p=param: ssm.delete_parameter(Name=p))

ec2 = c("ec2")
do("ec2 delete security group", SECURITY_GROUP,
   lambda: ec2.delete_security_group(GroupId=SECURITY_GROUP))

summary = os.environ.get("GITHUB_STEP_SUMMARY")
if summary:
    with open(summary, "a", encoding="utf-8") as fh:
        fh.write(f"## v2 cleanup ({MODE})\n\n```\n" + "\n".join(log) + "\n```\n")
        fh.write("\nUntouched by design: newsarchive alarms, CloudStruct ECS "
                 "task definition.\n")
