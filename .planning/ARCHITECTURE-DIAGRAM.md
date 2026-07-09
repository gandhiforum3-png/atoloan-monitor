# Atoloan — Full System Architecture

> For new engineers. Every component is real — sourced directly from the codebase.

---

## LOCAL DEVELOPMENT Architecture

```
YOUR LAPTOP
───────────────────────────────────────────────────────────────────────

  Terminal 1: Frontend
  ┌─────────────────────────────────────────────────────────────────┐
  │  Vite Dev Server   (node:22)                                    │
  │  URL:  http://localhost:5173                                    │
  │  Cmd:  cd atoloan-ui && npm run dev                             │
  │                                                                 │
  │  Hot-reload: edit a .tsx file → browser updates instantly       │
  │  VITE_API_URL=http://127.0.0.1:8000  (.env.development)         │
  │                                                                 │
  │  React 19 app (TypeScript source, NOT built/bundled)            │
  └─────────────────────┬───────────────────────────────────────────┘
                        │  API calls to http://127.0.0.1:8000
                        ▼

  Terminal 2: Backend
  ┌─────────────────────────────────────────────────────────────────┐
  │  uvicorn  (FastAPI, Python 3.12)                                │
  │  URL:  http://127.0.0.1:8000                                    │
  │  Cmd:  uvicorn app.main:app --reload --port 8000                │
  │                                                                 │
  │  --reload flag: edit a .py file → server restarts instantly     │
  │  Reads secrets from: .env file (NOT AWS Secrets Manager)        │
  │                                                                 │
  │  .env file contains:                                            │
  │    PGHOST=localhost   PGPORT=5432                               │
  │    PGUSER=atoloan     PGPASSWORD=atoloan                        │
  │    PGDATABASE=atoloan                                           │
  │    ANTHROPIC_API_KEY=sk-ant-...                                 │
  │    SEVENCREDIT_ACCOUNT=...  (test env)                          │
  └─────────────────────┬───────────────────────────────────────────┘
                        │  asyncpg connection to localhost:5432
                        ▼

  Terminal 3 (or Option A): Database
  ┌─────────────────────────────────────────────────────────────────┐
  │  OPTION A — docker-compose  (recommended)                       │
  │  Cmd: docker-compose up                                         │
  │                                                                 │
  │  Starts TWO containers:                                         │
  │  ┌───────────────────┐    ┌──────────────────────────────────┐  │
  │  │ postgres:16-alpine│    │  atoloan-api  (built from        │  │
  │  │ port: 5432        │    │  Dockerfile)                     │  │
  │  │ user: atoloan     │◄───│  port: 8000                      │  │
  │  │ db:   atoloan     │    │  PGHOST=db (docker network)      │  │
  │  │ volume: postgres_ │    └──────────────────────────────────┘  │
  │  │         data      │                                          │
  │  └───────────────────┘                                          │
  │                                                                 │
  │  OPTION B — local Postgres (already installed)                  │
  │  psql -U postgres -c "CREATE DATABASE atoloan;"                 │
  │  Then run uvicorn directly (Terminal 2 above)                   │
  └─────────────────────────────────────────────────────────────────┘

  What's MISSING locally vs prod:
  ✗ No K8s / k3s          (no pods, no namespaces, no ingress)
  ✗ No nginx              (Vite serves frontend directly)
  ✗ No TLS / HTTPS        (plain HTTP localhost)
  ✗ No cert-manager       (no SSL certificates)
  ✗ No External Secrets   (secrets come from .env file instead)
  ✗ No AWS Secrets Manager (no IAM role needed)
  ✗ No RDS               (plain Postgres on localhost or docker)
  ✗ No Route 53           (no DNS, just localhost)
  ✓ Same Python code      (uvicorn = same server used in prod)
  ✓ Same React code       (same components, different dev server)
  ✓ Same database schema  (SQLAlchemy auto-creates tables on startup)
```

---

## LOCAL vs AWS PROD — Side-by-Side Comparison

```
COMPONENT         LOCAL DEV                    AWS PROD
────────────────  ───────────────────────────  ──────────────────────────────
Frontend server   Vite dev server :5173        nginx in K8s pod :80
Frontend URL      http://localhost:5173        https://atoloans.com
Frontend build    No build — hot reload        Docker: node build → nginx serve
Backend server    uvicorn --reload :8000       uvicorn in K8s pod :8000 (×2)
Backend URL       http://127.0.0.1:8000        https://api.atoloans.com
TLS / HTTPS       None (HTTP only)             cert-manager + Let's Encrypt
Load balancer     None (1 process)             ingress-nginx + 2 replicas
Database          localhost:5432 or docker     AWS RDS us-east-2 (private subnet)
DB hostname       localhost                    postgres.atoloans.com (Route 53)
Secrets           .env file (gitignored)       AWS Secrets Manager → ESO → K8s Secret
Kubernetes        Not used                     k3s (server + agent EC2 t3.small)
File uploads      ./upload_pdf/               K8s PVC: upload-pdfs (5 Gi)
                  ./user_uploaded_documents/  K8s PVC: user-docs (5 Gi)
700Credit env     test                         prod
CORS              localhost:5173, :5174        https://atoloans.com
Replicas          1 (single process)           2 pods (zero-downtime rolling deploy)
Docker            Optional (docker-compose)    Required (images on Docker Hub)
Infrastructure    Nothing to provision         Terraform manages all AWS resources
```

---

## AWS PROD Architecture

## The Big Picture

```
 INTERNET
    │
    │  User visits atoloans.com
    ▼
┌──────────────────────────────────────────────────────────────────────┐
│                        AWS ROUTE 53 (DNS)                            │
│                        Domain: atoloans.com                          │
│                                                                      │
│   atoloans.com  ──────────────────────────►  Elastic IP (prod)      │
│   www.atoloans.com ───────────────────────►  Elastic IP (prod)      │
│   api.atoloans.com ───────────────────────►  Elastic IP (prod)      │
│   postgres.atoloans.com ──────────────────►  RDS endpoint (CNAME)   │
└──────────────────────────────────────────────────────────────────────┘
    │
    │  HTTPS traffic (port 443)
    ▼
┌──────────────────────────────────────────────────────────────────────┐
│                    AWS VPC  (10.2.0.0/16)  us-east-2                 │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │  Public Subnet A  (10.2.1.0/24)  us-east-2a                  │    │
│  │                                                              │    │
│  │  ┌──────────────────────────────────────────────────────┐    │    │
│  │  │  EC2: atoloan-k8s-prod-server  (t3.small)            │    │    │
│  │  │  Elastic IP ← Route 53 A records point here          │    │    │
│  │  │                                                      │    │    │
│  │  │  Runs:  k3s SERVER  (Kubernetes control plane)       │    │    │
│  │  │         ingress-nginx  (entry point for HTTP/HTTPS)  │    │    │
│  │  │         cert-manager   (free TLS from Let's Encrypt) │    │    │
│  │  │         external-secrets operator                    │    │    │
│  │  │                                                      │    │    │
│  │  │  Storage: 20 GB gp3  +  2 GB swap                    │    │    │
│  │  └──────────────────────────────────────────────────────┘    │    │
│  └──────────────────────────────────────────────────────────────┘    │
│                                                                      │
│  ┌─────────────────────────────────────────────────────────────┐     │
│  │  Public Subnet B  (10.2.2.0/24)  us-east-2b                 │     │
│  │                                                             │     │
│  │  ┌─────────────────────────────────────────────────────┐    │     │
│  │  │  EC2: atoloan-k8s-prod-agent  (t3.small)            │    │     │
│  │  │                                                     │    │     │
│  │  │  Runs:  k3s AGENT  (Kubernetes worker node)         │    │     │
│  │  │         Pods are scheduled here by k3s              │    │     │
│  │  │                                                     │    │     │
│  │  │  Storage: 20 GB gp3  +  2 GB swap                   │    │     │
│  │  └─────────────────────────────────────────────────────┘    │     │
│  └─────────────────────────────────────────────────────────────┘     │
│                                                                      │
│  ┌─────────────────────────────────────────────────────────────┐     │
│  │  Private Subnet A  (10.2.10.0/24)  us-east-2a               │     │
│  │  Private Subnet B  (10.2.11.0/24)  us-east-2b               │     │
│  │                                                             │     │
│  │  ┌─────────────────────────────────────────────────────┐    │     │
│  │  │  AWS RDS  PostgreSQL 16  (db.t4g.micro)             │    │     │
│  │  │  Name: atoloan-postgres-prod                        │    │     │
│  │  │  DB:   atoloandb                                    │    │     │
│  │  │  DNS:  postgres.atoloans.com  (Route 53 CNAME)      │    │     │
│  │  │                                                     │    │     │
│  │  │  NOT publicly accessible — only k3s nodes           │    │     │
│  │  │  can connect (enforced by RDS security group)       │    │     │
│  │  │  Storage: 20 GB gp3, encrypted, auto-scale to 100 GB│    │     │
│  │  │  Deletion protection: ON  (can't terraform destroy) │    │     │
│  │  └─────────────────────────────────────────────────────┘    │     │
│  └─────────────────────────────────────────────────────────────┘     │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Kubernetes Cluster (k3s) — What Runs Inside

```
k3s CLUSTER
├── Server node  (EC2 prod-server)  — control plane + ingress
└── Agent node   (EC2 prod-agent)   — workload pods

┌────────────────────────────────────────────────────────────────────────┐
│  NAMESPACE: ingress-nginx   (system, installed by Helm)                │
│  └── ingress-nginx-controller pod                                      │
│       • The "front door" for all HTTP/HTTPS traffic                    │
│       • Reads Ingress resources and routes to the right service        │
│       • Terminates TLS (passes decrypted traffic to pods)              │
└────────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────────┐
│  NAMESPACE: cert-manager    (system, installed by Helm)                │
│  └── cert-manager pod                                                  │
│       • Automatically gets free TLS certificates from Let's Encrypt    │
│       • ClusterIssuer: letsencrypt-prod                                │
│       • Stores certs as K8s Secrets (atoloan-ui-tls, atoloan-api-tls)  │
└────────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────────┐
│  NAMESPACE: external-secrets   (system, installed by Helm)             │
│  └── external-secrets-operator pod                                     │
│       • Watches ExternalSecret resources                               │
│       • Connects to AWS Secrets Manager using EC2 IAM role (no keys!)  │
│       • Creates real K8s Secrets that pods can use                     │
└────────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────────┐
│  NAMESPACE: atoloan-frontend-prod                                      │
│                                                                        │
│  ResourceQuota:                                                        │
│    CPU requests: 500m  limits: 1000m                                   │
│    Memory requests: 512Mi  limits: 1Gi                                 │
│    Max pods: 5                                                         │
│                                                                        │
│  Deployment: atoloan-ui                                                │
│  ├── 1 replica                                                         │
│  ├── Image: gandhiforum3/atoloan-ui:latest  (Docker Hub)               │
│  ├── Container: nginx serving static React build on port 80            │
│  └── Resources: 100m CPU / 128Mi RAM request                           │
│                                                                        │
│  Service: atoloan-ui  (ClusterIP, port 80)                             │
│                                                                        │
│  Ingress: atoloans.com, www.atoloans.com                               │
│  └── TLS certificate: atoloan-ui-tls (managed by cert-manager)         │
└────────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────────┐
│  NAMESPACE: atoloan-backend-prod                                       │
│                                                                        │
│  ResourceQuota:                                                        │
│    CPU requests: 2000m  limits: 4000m                                  │
│    Memory requests: 2Gi  limits: 4Gi                                   │
│    Max pods: 10                                                        │
│                                                                        │
│  ExternalSecrets (pull from AWS Secrets Manager every 1 hour):         │
│  ├── atoloan-postgres-secret  ← atoloan/postgres-prod                  │
│  │     keys: PGUSER, PGPASSWORD, PGHOST, PGPORT, PGDATABASE            │
│  ├── atoloan-openai-secret    ← atoloan/openai                         │
│  │     keys: OPENAI_API_KEY                                            │
│  └── atoloan-sevencredit-secret ← atoloan/sevencredit                  │
│        keys: SEVENCREDIT_ACCOUNT, SEVENCREDIT_PASSWORD, CLIENT_ID/SECRET│
│                                                                        │
│  ConfigMap: atoloan-api-config                                         │
│  └── APP_ENV=prod, SEVENCREDIT_ENV=prod                                │
│      CORS_ORIGINS=https://atoloans.com,https://www.atoloans.com        │
│                                                                        │
│  Deployment: atoloan-api                                               │
│  ├── 2 replicas  (RollingUpdate: maxSurge=1, maxUnavailable=0)         │
│  ├── Image: gandhiforum3/atoloan-api:latest  (Docker Hub)              │
│  ├── InitContainer: db-migrate (runs migrations before API starts)     │
│  ├── Container: uvicorn app.main:app on port 8000                      │
│  ├── Resources: 300m CPU / 512Mi RAM request  |  1CPU / 1Gi limit      │
│  ├── Readiness probe: GET /hello every 10s                             │
│  ├── Liveness probe: GET /hello every 30s                              │
│  └── Volumes:                                                          │
│       ├── /app/upload_pdf  ← PVC: upload-pdfs (5Gi, local-path)        │
│       └── /app/user_uploaded_documents ← PVC: user-docs (5Gi)          │
│                                                                        │
│  Service: atoloan-api  (ClusterIP, port 80 → 8000)                     │
│                                                                        │
│  Ingress: api.atoloans.com                                             │
│  └── TLS certificate: atoloan-api-tls (managed by cert-manager)        │
└────────────────────────────────────────────────────────────────────────┘
```

---

## How a User Request Flows Through the System

```
EXAMPLE: User submits loan pre-qualification form

 Browser (atoloans.com)
    │
    │  1. React app calls POST https://api.atoloans.com/findback
    │
    ▼
 Route 53
    │  Resolves api.atoloans.com → Elastic IP on k3s server EC2
    ▼
 ingress-nginx (k3s server EC2, port 443)
    │  TLS terminated here using cert-manager certificate
    │  Decrypted HTTP forwarded to backend Service
    ▼
 atoloan-api Service  (ClusterIP)
    │  Kubernetes load-balances between 2 backend pods
    ▼
 atoloan-api Pod  (FastAPI / uvicorn, port 8000)
    │
    ├─► 2. Calls 700Credit API  (external HTTPS)
    │       Sends applicant PII → gets credit pre-qualification result
    │
    ├─► 3. Queries RDS PostgreSQL  (postgres.atoloans.com:5432)
    │       find_best_bank() — looks up credit unions by zipcode + score
    │       save_findback_result() — saves user + loan application record
    │
    └─► 4. Returns JSON to browser
            { prequal: {...}, best_bank: {...}, application_id: 123 }
```

---

## Secret Management Flow

```
HOW SECRETS GET INTO PODS  (no hardcoded passwords anywhere)

  Terraform                AWS Secrets Manager
  ─────────                ──────────────────────────────────────
  secrets-prod.tf  ──────► atoloan/postgres-prod
  (on infra deploy)         { PGUSER, PGPASSWORD, PGHOST, ... }
                   ──────► atoloan/openai
                   ──────► atoloan/sevencredit

  EC2 IAM Role             External Secrets Operator
  ─────────────            ──────────────────────────────────────
  atoloan-ec2-prod-role    ClusterSecretStore
  Policy allows:    ──────► Uses EC2 IMDS (169.254.169.254)
  secretsmanager:           to authenticate as the EC2 IAM role
  GetSecretValue            NO ACCESS KEYS NEEDED

  ExternalSecret           K8s Secret                Pod env var
  ─────────────            ──────────                ───────────
  (refreshes every 1h)
  atoloan-postgres-secret  atoloan-postgres-secret   PGUSER=...
  pulls PGUSER        ───► key: PGUSER          ───► PGPASSWORD=...
  pulls PGPASSWORD         key: PGPASSWORD           PGHOST=...
  pulls PGHOST             key: PGHOST               (injected at
  pulls PGPORT             ...                        pod startup)
```

---

## Frontend Architecture

```
atoloan-ui  (React 19 + Vite 7)

 Source code (TypeScript + React)
    │
    │  npm run build  →  /app/dist/  (static HTML + JS + CSS)
    ▼
 Docker multi-stage build
 ├── Stage 1: node:22-alpine
 │     npm ci → vite build
 │     VITE_API_URL baked in at build time:
 │       prod:  https://api.atoloans.com
 │       dev:   http://127.0.0.1:8000
 │
 └── Stage 2: nginx:alpine
       Serves /app/dist as static files on port 80
       Uses custom nginx.conf

 Key libraries:
 ├── react-router-dom v7   — client-side page routing
 ├── chart.js v4           — data visualisation
 ├── react-markdown v10    — render markdown content
 └── remark-gfm            — GitHub-flavoured markdown tables/links

 Testing:
 ├── vitest       — unit tests
 └── playwright   — end-to-end browser tests
```

---

## Backend Architecture

```
atoloan-backend  (FastAPI + Python 3.12 + uvicorn)

 app/
 ├── main.py                  FastAPI app entry point
 │   ├── Registers all routers
 │   ├── CORS middleware (allows atoloans.com origins only in prod)
 │   └── Lifespan: creates DB tables + tests connection on startup
 │
 ├── core/
 │   ├── config.py            Settings (reads env vars / .env file)
 │   │   Secrets loaded from: env vars set by K8s ExternalSecret
 │   │   Keys: DATABASE_URL, PGHOST/PORT/USER/PASS, ANTHROPIC_API_KEY,
 │   │         SEVENCREDIT_*, CORS_ORIGINS
 │   └── dependencies.py      get_conn() — async DB connection per request
 │
 ├── db.py                    SQLAlchemy 2.0 async engine
 │   └── asyncpg driver (non-blocking Postgres queries)
 │
 ├── models/                  SQLAlchemy table definitions
 │   ├── user_table.py        users (email, name, address, phone)
 │   ├── loan_application_table.py  loan_applications (user FK, scores, result)
 │   ├── rate_sheet.py        credit union rate sheet tables
 │   └── prequal_result.py    pre-qualification result shape
 │
 ├── api/routers/             One file per feature area
 │   ├── health.py            GET /hello, GET /db-check, POST /echo
 │   ├── users.py             POST /users  (create account)
 │   ├── documents.py         POST /uploadDocuments  (driver's license + paycheck)
 │   ├── credit_unions.py     GET/DELETE /credit-unions, POST /update
 │   ├── rate_sheets.py       POST /ratesheetuploader, POST /parse-rate-sheet-markdown
 │   └── findback.py          POST /findback, POST /validate-zipcode
 │
 ├── services/                Business logic (no HTTP here)
 │   ├── bank_finder.py       find_best_bank() — matches applicant to best rate
 │   ├── loan_application_mutations.py  save_findback_result()
 │   ├── credit_union_*.py    CRUD for credit union data
 │   ├── pdf_parser*.py       PDF → markdown (camelot + pdfplumber + OCR)
 │   ├── pdf_validator.py     Validate parsed markdown vs original PDF
 │   └── rate_sheet_parser.py  Markdown → structured JSON (uses Claude AI)
 │
 └── integrations/
     └── seven_hundred.py     700Credit API client
                              Sends applicant data, gets credit pre-qual result

 AI Integration:
 └── rate_sheet_parser.py uses ANTHROPIC_API_KEY
     Claude parses unstructured rate sheet markdown → structured JSON
     (CreditUnionRateSheet Pydantic model)

 PDF Processing stack:
 ├── camelot-py     extract tables from PDF
 ├── pdfplumber     extract text from PDF
 ├── pytesseract    OCR for image-based PDFs
 ├── poppler-utils  PDF rendering (system dep)
 └── ghostscript    PDF handling (system dep)
```

---

## Full API Reference

```
BASE URL (prod):  https://api.atoloans.com
BASE URL (local): http://127.0.0.1:8000

HEALTH
  GET  /hello                      → {"message": "hello world"}
  GET  /db-check                   → {"status": "ok"} or 500
  POST /echo                       → echoes JSON body back

USERS
  POST /users
    Body: { email, first_name, last_name, address, city, state, zipcode, phone_number }
    Returns: { status: "created", email }
    Error: 409 if email already exists

DOCUMENTS
  POST /uploadDocuments
    Body: multipart/form-data
      drivers_license: file
      paycheck: file
      user_name: string (used for folder naming)
    Returns: { status, submission_id, files: [...] }
    Saved to: /app/user_uploaded_documents/{timestamp}_{name}/

CREDIT UNIONS
  GET    /credit-unions                → list all { id, name }
  GET    /credit-unions/{id}/ratesheet → full rate sheet for one credit union
  DELETE /credit-unions/{id}          → delete credit union + all its data
  POST   /update                      → upsert full rate sheet payload

RATE SHEETS (PDF Upload + AI Parsing)
  POST /ratesheetuploader
    Body: multipart/form-data with PDF file
    Steps: save PDF → parse to markdown → validate → Claude AI → structured JSON
    Returns: { status, files: [{ filename, parser_used, validation, rate_sheet }] }

  GET  /ratesheetuploader/markdown/{filename}
    Returns raw parsed markdown file for download

  POST /parse-rate-sheet-markdown
    Body: { markdown_text: string, current_year: int }
    Returns: { status: "success", result: CreditUnionRateSheet }

LOAN PRE-QUALIFICATION
  POST /findback
    Body: { contactInfo: { firstName, lastName, email, phone, address, city, state, zip },
            bureau: "TU"|"EQ"|"EX",
            ssn: string,
            otherDownPayment: number,
            answers: { "down-payment": "5000-10000" } }
    Steps:
      1. Call 700Credit API → get pre-qualification result
      2. find_best_bank() → match to best credit union rate
      3. save_findback_result() → save to DB
    Returns: { prequal: {...}, best_bank: {...}, application_id: int }

  POST /validate-zipcode
    Body: { zipcode: string }
    Returns: { zipcode, valid: bool, city: string|null }
```

---

## Infrastructure as Code (Terraform)

```
atoloan-infra/
└── infrastructure/
    ├── dns/                    Route 53 hosted zone (deploy ONCE, never destroy)
    │   └── dns.tf              Creates atoloans.com zone in Route 53
    │                           You copy 4 nameservers → GoDaddy custom NS
    │
    └── prod/                   All prod AWS resources
        ├── provider.tf         AWS provider, us-east-2 region
        ├── vpc-prod.tf         VPC 10.2.0.0/16 + subnets + IGW + route tables
        ├── ec2-prod.tf         2× t3.small EC2, security group, Elastic IP
        │                       user_data installs k3s + helm + ingress/cert-manager/ESO
        │                       null_resource deploys all K8s manifests after cluster ready
        ├── rds-prod.tf         PostgreSQL 16 db.t4g.micro, private subnet, encrypted
        ├── secrets-prod.tf     Secrets Manager: postgres-prod, openai, sevencredit
        ├── iam-ec2-prod.tf     IAM role + policy: EC2 → Secrets Manager read access
        └── dns-records-prod.tf A records: atoloans.com → EIP, CNAME: postgres.atoloans.com

 Kubernetes manifests (applied by Terraform null_resource):
 k8s/aws-prod/
 ├── namespaces-aws-prod.yaml          atoloan-frontend-prod, atoloan-backend-prod
 ├── namespace-quotas-prod.yaml        CPU/memory/pod limits per namespace
 ├── secret-store-aws-prod.yaml        ClusterSecretStore (uses EC2 IMDS, no keys)
 ├── backend-external-secret-aws-prod.yaml  ExternalSecrets: postgres, openai, 700credit
 ├── frontend-aws-prod.yaml            Deployment + Service for React (nginx)
 ├── backend-aws-prod.yaml             Deployment + Service + Ingress for FastAPI
 │                                     (also contains ExternalSecrets + ConfigMap + PVCs)
 ├── cert-manager-issuer-prod.yaml     ClusterIssuer: letsencrypt-prod
 └── frontend-ingress-prod.yaml        Ingress: atoloans.com → frontend service
```

---

## Security Architecture

```
Layer           What protects it             How
──────────────  ───────────────────────────  ────────────────────────────────────
DNS             Route 53                     AWS-managed, not GoDaddy DNS
TLS/HTTPS       cert-manager + Let's Encrypt Auto-renewed free certificates
Network         AWS Security Group (k8s-sg)  Only ports 80/443/22/6443 open
Database        AWS Security Group (rds-sg)  Port 5432 ONLY from k8s nodes
Secrets         AWS Secrets Manager          No plaintext secrets in code/git
Secret access   IAM role on EC2              No access keys — uses IMDS metadata
K8s secrets     External Secrets Operator    Pulls from Secrets Manager, creates K8s Secrets
Pod isolation   K8s namespaces + ResourceQuota  Each service in its own namespace
API CORS        FastAPI CORSMiddleware        Only atoloans.com origins allowed
DB access       asyncpg connection pool      Connection reused, not opened per request
RDS protection  deletion_protection=true     Terraform cannot accidentally drop the DB
PDF uploads     Path traversal check         download_markdown() validates path stays in dir
```

---

## Environments

```
ENVIRONMENT   FRONTEND URL              API URL                    DATABASE
────────────  ────────────────────────  ─────────────────────────  ─────────────────────
local dev     http://localhost:5173     http://127.0.0.1:8000      postgres on localhost
              (vite dev server)         (uvicorn directly)         or docker-compose db

UAT           (uat infra)              (uat infra)                AWS RDS (uat)
              k3s on UAT EC2           k3s on UAT EC2             atoloan-uat/

Production    https://atoloans.com     https://api.atoloans.com   AWS RDS prod
              nginx in K8s pod         uvicorn in K8s pod (×2)    postgres.atoloans.com
```

---

## How to Run Locally (for new developers)

```bash
# 1. Backend
cd atoloan-backend/atoloan-backend
cp .env.example .env                  # fill in your local values
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Option A: docker-compose (starts Postgres + API together)
docker-compose up

# Option B: manual (if you have Postgres running locally)
uvicorn app.main:app --reload --port 8000

# 2. Frontend (in another terminal)
cd atoloan-ui
npm install
npm run dev                           # starts at http://localhost:5173
# VITE_API_URL=http://127.0.0.1:8000 is set in .env.development automatically

# 3. Tests
cd atoloan-backend/atoloan-backend
pytest                                # unit + integration tests

cd atoloan-ui
npm run test:unit                     # vitest
npm run test:e2e                      # playwright (needs running app)
```

---

## What Atoloan Monitor Will Watch

```
When atoloan-monitor is built (Phase 1+), it adds a 4th EC2 instance
that observes all of the above:

  atoloan-monitor EC2
  (IAM role: read + safe ops)
        │
        ├──► K8s Watch API ──► atoloan-frontend-prod pods
        │                      atoloan-backend-prod pods
        │                      OOMKills, restarts, pending, quota breaches
        │
        ├──► CloudWatch ──────► EC2 CPU/memory/disk/network
        │                       RDS connections, latency, storage
        │
        ├──► Prometheus ──────► FastAPI request latency, error rates
        │     (to be deployed)  per-endpoint p50/p95/p99
        │
        ├──► Postgres ────────► pg_stat_activity, slow queries,
        │     (RDS, read-only)  connection pool, lock contention
        │
        └──► AWS APIs ────────► Security group changes
                                Secrets Manager expiry
                                CloudTrail events at anomaly onset
```

---
*Generated: 2026-06-14 | Sources: atoloan-backend, atoloan-ui, atoloan-infra repos*
