# NovaDrive

A Google Drive-like cloud storage app running entirely on AWS Lambda — fully serverless, no EC2, no servers to manage. Push to `novadrive` and it deploys itself.

**Live stack:** Django 5 → Mangum → AWS Lambda → API Gateway v2 → CloudFront → S3  
**Auth:** Single-admin session auth — credentials stored in Lambda env vars, compared via `hmac.compare_digest`  
**UI:** Alpine.js + Tailwind CSS (CDN — no build step)  
**Domain:** `drive.nodepulsecaringal.xyz` (custom domain via ACM + Cloudflare + API Gateway)  
**Region:** Sydney (`ap-southeast-2`)

---

## Architecture

```
Browser (drive.nodepulsecaringal.xyz)
  └── Cloudflare (proxied — DDoS protection, hides AWS URL)
        └── API Gateway v2 (HTTP API, custom domain, $default catch-all)
              └── Lambda (Python 3.12 — Django 5 + Mangum)
                    ├── Admin auth         (single-admin — ADMIN_EMAIL + ADMIN_PASSWORD env vars)
                    ├── Neon PostgreSQL    (file + folder metadata, batch job tracking)
                    ├── S3                 (file storage — private, per-user folder prefix)
                    │     └── Direct browser upload via presigned POST URL
                    │         (file bytes never pass through Lambda)
                    ├── CloudFront         (signed URLs — only way to read files)
                    ├── SSM Parameter Store (DB URL, Resend key, CloudFront RSA key)
                    ├── AWS Batch / Fargate (folder zip jobs — bypasses Lambda timeout)
                    └── Resend             (transactional email — archive / restore notifications)

S3 Event → Lambda (notify.py)
  └── ObjectRestore:Completed → update DB restore_status → send "ready" email via Resend

Terraform Remote State
  ├── S3 Bucket (maangasserverless) — Intelligent-Tiering
  └── DynamoDB Table (terraform-state-lock) — state locking
```

### Upload flow

```
1. Browser → Lambda:  POST /drive/upload-url/   → presigned POST URL + s3_key + exists flag
2. Lambda → Browser:  { url, fields, s3_key, exists }  (signed by IAM, 5-min TTL)
   └── If exists=true: overwrite confirmation modal shown before continuing
3. Browser → S3:      POST directly (XHR — for progress events)
4. Browser → Lambda:  POST /drive/confirm/       → head_object, save/update DriveFile row
5. Browser:           file row injected into DOM (or empty-state removed on first upload)
```

### View / download flow

```
1. Browser → Lambda:  GET /drive/view/<id>/url/
2. Lambda → SSM:      fetch CloudFront private key (cached after first cold start)
3. Lambda:            sign URL with RSA key (1-hour expiry)
4. Browser:           receives signed CloudFront URL — plays/renders inline or downloads
```

### Folder zip flow

```
1. Browser → Lambda:  POST /drive/folders/zip/  → submit AWS Batch job(s)
2. Lambda → Batch:    submit_job() with FOLDER_IDS, OWNER_SUB, JOB_DB_ID env vars
3. Fargate container: worker.py — BFS walk folder tree, stream files from S3, write zip, upload to temp-zips/
4. Worker → DB:       progress updates every 5% (0-90%), then 95%, then 100% + READY status
5. Browser:           polls /drive/job/<id>/status/ every 3s — progress bar in toast
6. Browser:           on READY, triggers hidden iframe download of signed zip URL
7. Zip expires:       24 hours (BatchJob.expires_at)
```

---

## Branches

| Branch | Description |
|--------|-------------|
| `hello-world` | Minimal Django hello world |
| `CRUD` | Django CRUD with HTMX inline edits |
| `auth` | AWS Cognito authentication flows |
| `novadrive` | Full file storage app (Cognito auth) |
| `v2-novadrive` | Full file storage app — Cognito auth, Alpine.js search |
| `v3-novadrive` | **Current** — single-admin auth (no Cognito), Alpine.js throughout |

CI/CD triggers on push to `v3-novadrive`. See [.github/workflows/deploy.yml](.github/workflows/deploy.yml).

---

## Features

### File Management
| Feature | Detail |
|---------|--------|
| **Upload** | Direct-to-S3 via presigned POST (XHR progress bar). File bytes never touch Lambda. |
| **Overwrite confirmation** | If a file with the same name exists, a modal asks before overwriting. Concurrent uploads each get their own serialised confirmation. |
| **Download** | Signed 1-hour CloudFront URL. Never a public S3 link. |
| **Preview** | Images, video (inline player), audio, PDF (iframe), with fallback download prompt for other types. |
| **Rename** | Renames DB record + migrates S3 key. Extension is read-only in the modal. |
| **Soft delete** | Moves to Recycle Bin (30-day recovery window). |
| **Bulk delete** | Select multiple, delete in one request. |
| **Bulk archive** | Select multiple files, move all to Glacier Deep Archive. |

### Folder Management
| Feature | Detail |
|---------|--------|
| **Create** | Nested folders with parent FK. |
| **Rename** | Updates DB + recursively migrates all descendant file S3 keys. |
| **Delete** | Soft delete — appears in Recycle Bin. |
| **Navigate** | Click to enter; breadcrumb trail shows full path. |
| **Sidebar** | Folder tree in sidebar. Responsive — collapses to hamburger on mobile. |
| **Zip download** | AWS Batch compresses entire folder tree; progress toast tracks %. |

### Storage Tiers
| Tier | Storage Class | Retrieval | How to get there |
|------|--------------|-----------|-----------------|
| **Glacier Instant Retrieval** | `GLACIER_IR` | Instant | Default for all uploads |
| **Deep Archive** | `DEEP_ARCHIVE` | 12–48 hours | User-triggered "Archive" action |

Files in Deep Archive can be restored (request → 12-48h → ready email → 7-day download window → re-archives automatically).

### Archive & Restore
- Archive single or multiple files to Deep Archive
- Restore request via UI → S3 initiates retrieval
- S3 `ObjectRestore:Completed` event → `notify.py` Lambda → updates DB + sends "ready" email via Resend
- 7-day restore window before automatic re-archive
- Archive view shows all archived files with restore status badges

### Recycle Bin
- 30-day soft-delete recovery window for files and folders
- Days-remaining countdown (colour-coded: green → yellow → red)
- Restore individual items or bulk-restore selection
- Permanently delete from bin (beyond recovery)

### UI / UX
- **List + Grid view** toggle (persisted per session)
- **Image thumbnails** via CloudFront signed URL — spinner while loading, fade-in on load
- **Video thumbnails** — `<video preload="metadata">` renders first frame; spinner + `onerror` fallback for archived files
- **Video player** — tap left/right thirds to seek ±10s; keyboard `←` / `→` seek, `Space` play/pause, `Esc` close
- **Context menu** — right-click files/folders; items shown/hidden based on selection type and count
- **Selection-aware labels** — "Delete files (3)", "Download as Zip (1)", etc.
- **Toast stack** — progress, success, and error toasts; zip jobs show live progress bar
- **Search** — searches files and folders by name globally (Alpine.js fetch, debounced 300ms)
- **Responsive sidebar** — full sidebar on desktop; hamburger drawer on mobile with backdrop

---

## Project Structure

```
.
├── .github/
│   └── workflows/
│       └── deploy.yml              # CI/CD — triggers on novadrive branch push
├── lambda/
│   └── serverless_web_app/
│       ├── wsgi.py                 # Mangum ASGI handler (Lambda entry point)
│       ├── notify.py               # Lambda handler for S3 ObjectRestore:Completed events
│       ├── manage.py
│       ├── requirements.txt
│       ├── config/
│       │   ├── urls.py
│       │   └── settings/
│       │       ├── base.py         # Shared settings, SSM fetches, AWS config
│       │       ├── dev.py          # DEBUG=True, no SSM
│       │       └── prod.py         # Secure headers, CSRF, session cookies
│       ├── accounts/               # Single-admin auth app
│       │   ├── decorators.py       # @cognito_login_required (checks session key)
│       │   ├── forms.py            # SignInForm only
│       │   ├── views.py            # signin, signout, dashboard
│       │   └── urls.py
│       ├── drive/                  # Main storage app
│       │   ├── models.py           # DriveFile, DriveFolder, BatchJob
│       │   ├── views.py            # All drive views (upload, download, rename, archive, restore, zip…)
│       │   ├── urls.py             # 27 endpoints
│       │   └── migrations/
│       ├── batch/
│       │   ├── Dockerfile          # python:3.12-slim Fargate image
│       │   ├── requirements.txt
│       │   └── worker.py           # zip_folder job — BFS walk, S3 stream, zipfile, upload
│       ├── templates/
│       │   ├── base.html           # Layout, footer, navbar
│       │   ├── accounts/           # signup, verify, signin, dashboard, forgot/reset
│       │   └── drive/
│       │       ├── home.html       # Main drive UI (Alpine.js driveApp())
│       │       ├── archived.html   # "File is archived" info page
│       │       └── partials/
│       │           ├── file_row.html          # File row (list + grid, thumbnails, spinners)
│       │           ├── folder_row.html        # Folder row (list + grid)
│       │           ├── recycle_row.html       # Bin file row (thumbnails, days-left badge)
│       │           ├── recycle_folder_row.html
│       │           ├── search_results.html    # Search results partial (rendered by Alpine.js fetch)
│       │           └── sidebar_folder.html    # Sidebar folder tree node
│       └── static/
├── terraform-state/                # Run once to bootstrap S3 + DynamoDB state backend
├── apigateway.tf                   # HTTP API, throttling, $default route
├── cloudfront_drive.tf             # CloudFront distribution (OAC, signed URLs)
├── cloudfront_keys.tf              # RSA key pair → SSM + CloudFront key group
├── cloudwatch.tf                   # Log group (14d), 4xx alarm, spike alarm
├── custom_domain.tf                # ACM cert (DNS validation) + API Gateway custom domain
├── iam.tf                          # Lambda execution role + SSM/S3/CloudFront policies
├── lambda.tf                       # Lambda function + env vars
├── outputs.tf
├── s3_drive.tf                     # Drive bucket (private, versioned, CORS, OAC policy)
├── sns.tf                          # SNS topic + email subscription
├── ssm.tf                          # SSM SecureString for DB URL
├── variables.tf
├── versions.tf                     # Terraform version + S3 backend
└── env.example                     # GitHub Actions secrets reference (all required secrets listed)
```

---

## Models

### DriveFolder
```
owner_sub       CharField(128)    Owner identifier — always "admin" in v3
name            CharField(255)
parent          ForeignKey(self)  Nullable — root folders have no parent
created_at      DateTimeField     Auto
deleted_at      DateTimeField     Nullable — soft delete (30-day bin)
Unique: (owner_sub, parent, name)
```

### DriveFile
```
owner_sub           CharField(128)    Owner identifier — always "admin" in v3
folder              ForeignKey(DriveFolder, nullable)
name                CharField(255)
s3_key              CharField(512, unique)   {sub}/{folder_path}/{filename}
size                BigIntegerField
content_type        CharField
storage_class       CharField    GLACIER_IR | DEEP_ARCHIVE
uploaded_at         DateTimeField
restore_status      CharField    '' | 'pending' | 'ready'
restore_expires_at  DateTimeField   Nullable — 7 days after ready
restore_notify_email EmailField
deleted_at          DateTimeField   Nullable — soft delete
```

### BatchJob
```
job_id       CharField(128)   AWS Batch job ID
type         CharField        zip_folder
owner_sub    CharField(128)
folder_name  CharField(255)   For display in toasts
status       CharField        pending | running | ready | failed
result_key   CharField(512)   S3 key of output zip
progress     IntegerField     0–100
created_at   DateTimeField
expires_at   DateTimeField    Nullable — 24h after READY
```

---

## API Endpoints

### Auth (`accounts/urls.py`)
```
GET/POST  /signin/              Sign in (hmac credential check, sets session)
GET       /signout/             Flush session
GET       /dashboard/           Account info
```

### Drive (`drive/urls.py`)
```
GET   /drive/                              My Drive (root)
GET   /drive/folder/<pk>/                 Browse folder

POST  /drive/folder/create/               Create folder
POST  /drive/folder/<pk>/rename/          Rename folder + migrate S3 keys
POST  /drive/folder/<pk>/delete/          Soft-delete folder
POST  /drive/folder/<pk>/zip/             Submit Batch zip job (single folder)
POST  /drive/folders/zip/                 Submit Batch zip jobs (JSON, multiple folders)
GET   /drive/job/<id>/status/             Poll zip job progress

POST  /drive/upload-url/                  Get presigned POST URL
POST  /drive/confirm/                     Confirm upload, save metadata
POST  /drive/file/<pk>/rename/            Rename file + migrate S3 key

GET   /drive/download/<pk>/               Presigned GET download (Content-Disposition: attachment)
GET   /drive/view/<pk>/url/               Signed CloudFront URL (JSON — for inline preview)
GET   /drive/view/<pk>/                   Redirect to signed URL
GET   /drive/thumb/<pk>/                  Signed CloudFront URL for thumbnail (images + video only)

POST  /drive/delete/<pk>/                 Soft-delete file
POST  /drive/bulk-delete/                 Bulk soft-delete (JSON)

POST  /drive/archive/                     Archive file(s) to Deep Archive (JSON)
GET   /drive/archive/view/                Archive view

POST  /drive/restore/<pk>/                Initiate Glacier restore
POST  /drive/restore/                     Bulk restore (JSON)

GET   /drive/bin/                         Recycle Bin
POST  /drive/bin/<pk>/restore/            Restore file from bin
POST  /drive/bin/<pk>/delete/             Permanently delete file
POST  /drive/bin/folder/<pk>/restore/     Restore folder from bin
POST  /drive/bin/bulk-restore/            Bulk restore from bin (JSON)
POST  /drive/bin/bulk-delete/             Bulk permanent delete (JSON)
```

---

## Environment Variables (Lambda)

| Variable | Description |
|----------|-------------|
| `DJANGO_SETTINGS_MODULE` | `config.settings.prod` |
| `ADMIN_EMAIL` | Single admin email address |
| `ADMIN_PASSWORD` | Single admin password (set via Terraform variable) |
| `DRIVE_BUCKET_NAME` | S3 bucket for user files |
| `CLOUDFRONT_DOMAIN` | CloudFront distribution domain |
| `CLOUDFRONT_KEY_PAIR_ID` | CloudFront public key ID |
| `CLOUDFRONT_PRIVATE_KEY_SSM_NAME` | SSM path for RSA private key (PEM) |
| `SSM_DATABASE_URL_NAME` | SSM path for Neon PostgreSQL URL |
| `BATCH_JOB_QUEUE` | AWS Batch job queue name |
| `BATCH_JOB_DEFINITION` | AWS Batch job definition name |
| `AWS_REGION` | e.g. `ap-southeast-2` |
| `ALLOWED_HOSTS` | `drive.nodepulsecaringal.xyz` |
| `CSRF_TRUSTED_ORIGINS` | `https://drive.nodepulsecaringal.xyz` |

Secrets (`DATABASE_URL`, Resend API key, CloudFront RSA private key) are stored in SSM Parameter Store as encrypted `SecureString` values — they never appear in the console or CI logs.

---

## S3 Key Structure

Every file is stored under the owner's sub (fixed string `"admin"` for the single-admin setup), with the full folder path reflected:

```
{owner_sub}/                          ← root files
{owner_sub}/photos/                   ← files in "photos" folder
{owner_sub}/photos/vacation/          ← files in nested folder
temp-zips/{uuid}.zip                  ← Batch-generated zips (24h expiry)
```

Renaming a folder cascades an `_s3_move` (copy + delete) across all descendant files, keeping S3 in sync with the DB hierarchy.

---

## Custom Domain Setup

```
Cloudflare DNS (proxied)
  └── CNAME drive → <api-gateway-regional-domain>.execute-api.ap-southeast-2.amazonaws.com
        └── API Gateway custom domain (TLS terminated by ACM cert)
              └── API mapping → serverless_web_app stage (no path prefix)
```

### Steps

**1. Run `terraform apply`** — creates ACM cert and API Gateway custom domain.

**2. Add ACM validation CNAME in Cloudflare (proxy OFF)**

| Field | Value |
|-------|-------|
| Type | CNAME |
| Name | `_abc123.drive` (from `terraform output acm_validation_cname_name`) |
| Target | `_xyz789.acm-validations.aws.` (from `terraform output acm_validation_cname_value`) |
| Proxy | **DNS only** (grey cloud) |

ACM needs to resolve the raw DNS record. Cloudflare proxy blocks this.

**3. Wait for cert** — `aws acm describe-certificate ... --query 'Certificate.Status'` → `"ISSUED"`

**4. Add drive CNAME (proxy ON)**

| Field | Value |
|-------|-------|
| Type | CNAME |
| Name | `drive` |
| Target | `terraform output cloudflare_cname_target` |
| Proxy | **Proxied** (orange cloud) |

---

## First-Time Setup

### 1. Bootstrap Terraform state backend

```bash
cd terraform-state
terraform init
terraform apply
```

> Change `maangasserverless` in `terraform-state/variables.tf` and `versions.tf` if the bucket name is taken.

### 2. Add GitHub Actions secrets

Go to **GitHub repo → Settings → Secrets and variables → Actions** and add every secret listed in [env.example](env.example).

| Secret | Description |
|--------|-------------|
| `AWS_ACCESS_KEY_ID` | IAM access key |
| `AWS_SECRET_ACCESS_KEY` | IAM secret key |
| `DATABASE_URL` | Neon connection string (used by CI migrations and Terraform) |
| `RESEND_API_KEY` | Resend API key for transactional email |
| `ADMIN_EMAIL` | Admin login email address |
| `ADMIN_PASSWORD` | Admin login password — seeded into SSM on first deploy, never overwritten after |
| `DOCKERHUB_USERNAME` | Docker Hub username (for pushing the Batch worker image) |
| `DOCKERHUB_TOKEN` | Docker Hub access token |

IAM user needs: Lambda, API Gateway, IAM, S3, DynamoDB, SSM, CloudFront, ACM, SNS, Batch.

> `terraform.tfvars` is not used — all values come from GitHub Actions secrets via `TF_VAR_*` env vars.

### 3. Deploy

```bash
git push origin v3-novadrive
```

GitHub Actions: install deps → `manage.py migrate` → `terraform plan` → `terraform apply`.

### 4. Confirm SNS subscription

AWS sends a confirmation email after the first deploy. Click **Confirm subscription** or alarms won't deliver.

---

## CI/CD Pipeline

```
push to v3-novadrive
    ├── pip install -r requirements.txt -t lambda/serverless_web_app/   (vendor deps for Lambda zip)
    ├── pip install -r requirements.txt                                   (for manage.py)
    ├── python manage.py migrate
    ├── terraform init
    ├── terraform plan -out=tfplan
    └── terraform apply -auto-approve tfplan
```

---

## Local Development

```bash
cd lambda/serverless_web_app
pip install -r requirements.txt
export DATABASE_URL="postgresql://..."
python manage.py migrate
python manage.py runserver
```

Uses `config.settings.dev` (DEBUG=True, no SSM). Set `ADMIN_EMAIL` and `ADMIN_PASSWORD` env vars for auth, and real `AWS_*` env vars if you need S3 locally.

---

## Rate Limiting

API Gateway stage throttling:

```hcl
throttling_rate_limit  = 100   # sustained req/s
throttling_burst_limit = 200   # spike req/s
```

Requests over limit → `429 Too Many Requests` from API Gateway. Lambda never invoked, DB never touched, no charge.

---

## Alerting

SNS email alerts. Defined in `sns.tf` + `cloudwatch.tf`.

| Alarm | Metric | Threshold | Signal |
|-------|--------|-----------|--------|
| `4xx-errors` | `4XXError` (API GW) | > 50 in 1 min | Throttling firing |
| `request-spike` | `Count` (API GW) | > 500 in 1 min | High traffic |

---

## Estimated AWS Costs

| Service | Free Tier | After free tier |
|---------|-----------|-----------------|
| Lambda | 1M req/mo | ~$0.20/1M req |
| API Gateway | 1M req/mo | ~$1.00/1M req |
| CloudFront | 1TB transfer + 10M req/mo | ~$0.0085/GB |
| S3 (Glacier IR) | — | ~$0.004/GB/mo |
| S3 (Deep Archive) | — | ~$0.00099/GB/mo |
| AWS Batch / Fargate | — | ~$0.04048/vCPU-hr |
| ACM Certificate | Free | Always free |
| CloudWatch Logs | 5GB ingestion | ~$0.50/GB |
| SSM Parameter Store | 10,000 API calls/mo | $0 (standard) |
| SNS | 1,000 emails/mo | ~$2.00/100k |
| Neon PostgreSQL | 0.5GB | $0 on free tier |
| S3 (Terraform state) | — | < $0.01/mo |
| DynamoDB (lock table) | 25GB + 200M req | $0 for this use case |

---

## Key Design Decisions

**Why direct S3 upload?**
API Gateway has a 6MB payload limit and Lambda a 6MB response limit. Presigned POST URLs let the browser upload directly to S3 — Lambda only generates the URL and confirms afterward. File bytes never touch Lambda or API Gateway.

**Why XHR instead of fetch() for uploads?**
The Fetch API does not expose upload progress events. `XMLHttpRequest.upload` fires `progress` events as bytes are sent, enabling the per-file progress bar.

**Why CloudFront signed URLs?**
The S3 bucket is fully private (no public access). CloudFront is the only read path (OAC). Signed URLs expire after 1 hour and are tied to the authenticated session — no permanent shareable links.

**Why SSM SecureString for the CloudFront private key?**
The RSA private key grants ability to sign all CloudFront URLs. Storing it as a Lambda env var exposes it in the AWS console. SSM SecureString encrypts it with KMS and Lambda reads it via IAM role at cold start, then caches it in a module-level global.

**Why AWS Batch for folder zips?**
Lambda has a 15-minute execution timeout and limited memory. Large folder trees can take longer to zip and upload. Batch runs a Fargate container with no time limit, writes progress to the DB, and the frontend polls for completion.

**Why soft delete (Recycle Bin) instead of immediate S3 deletion?**
Accidental deletes are common. A 30-day recycle bin lets users recover files without contacting support. The DB `deleted_at` field gates all queries; the S3 object is only deleted on permanent deletion.

**Why single-admin instead of Cognito?**
For a personal cloud drive with one owner, Cognito adds cost, complexity (User Pool, App Client, hosted UI, token refresh), and dependencies. A single `hmac.compare_digest` check against env-var credentials — encrypted at rest by AWS Lambda — achieves the same session security at zero extra cost and with far less infrastructure.

**Why Cloudflare proxy ON?**
Public DNS returns Cloudflare IPs, not the raw API Gateway domain — DDoS protection at the edge and AWS infrastructure is not exposed.

**Why proxy OFF for the ACM validation CNAME?**
ACM resolves the validation CNAME directly. Cloudflare's proxy returns its own IP instead of the expected value, breaking validation. The record must be DNS-only until the cert is issued.

---

## Troubleshooting

### S3 Direct Upload CORS Failure

**Symptom:** `No 'Access-Control-Allow-Origin' header` in browser console, upload stuck at 0%.

**Root cause:** boto3's `generate_presigned_post` defaults to the global S3 endpoint (`s3.amazonaws.com`). The browser's XHR hits the global endpoint, which redirects to the regional one. Chrome sends a CORS preflight before following the redirect — the global endpoint doesn't return CORS headers. Preflight fails.

**Fix:** Pin the S3 client to the regional endpoint:

```python
boto3.client(
    "s3",
    region_name=settings.AWS_REGION,
    endpoint_url=f"https://s3.{settings.AWS_REGION}.amazonaws.com",
    config=Config(signature_version="s3v4"),
)
```

**Verify:** Upload log should show `https://s3.ap-southeast-2.amazonaws.com/...` (regional), not `https://<bucket>.s3.amazonaws.com/` (global). S3 response `204` = success.

### Video thumbnail spinner never hides

**Cause:** `drive_thumb` returns 404 for Deep Archive files (not accessible without restore). The `<video>` element fires `onerror` on 404, but without a handler the spinner stays.

**Fix:** All video thumbnail elements have both `onloadedmetadata` (success) and `onerror` (failure) handlers that dismiss the spinner.

### Batch zip job stuck in `pending`

Check CloudWatch logs for the Batch job definition. Common causes:
- Fargate container can't reach SSM (check VPC/subnet config and IAM policy)
- `SSM_DATABASE_URL_NAME` env var not set on the job definition
- Docker image not updated after a `worker.py` change

### Rename fails silently

If `_s3_move` (copy + delete) partially fails (copy succeeds, delete fails), the DB row is updated but the old S3 key still exists. Check CloudWatch logs for `s3.copy_object` or `s3.delete_object` errors against the Lambda IAM policy.
