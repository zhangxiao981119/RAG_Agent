# Enterprise Knowledge Base Q&A AI Agent

A privately deployed RAG Q&A system for enterprise knowledge bases. Upload PDF / Word / Excel / Markdown / plain text documents, and they are automatically parsed, chunked, and vectorized. Supports multiple knowledge bases, multi-turn conversations, permission isolation, clearance-based masking, audit tracing, quota limiting, sensitive word filtering, and gray-scale feature flags.

---

## 1. Quick Start (Local · 10 minutes)

**Prerequisites**: Docker Desktop (Windows/macOS) or Docker + Docker Compose (Linux), with at least **16 GB RAM**.

```bash
# 1. Enter the project directory
cd code

# 2. Optional: edit .env (change LLM API key, admin password, ports)
#    Skipping this is fine — the defaults will run out of the box

# 3. One-command startup (first run takes ~10-30 minutes depending on model download speed)
docker compose up -d --build

# 4. Watch the progress
docker compose logs -f api          # "Uvicorn running on ...:8000" means startup is complete

# 5. Open in browser
#    Frontend: http://localhost:5173
#    Backend API: http://localhost:8000/api/health  (all green = healthy)
#    MinIO console: http://localhost:9001  (raw document storage)
```

On first startup, the system automatically:
- Runs database migrations (0001 ~ 0007)
- Seeds the default tenant + admin role + admin account + public knowledge base
- Downloads bge-m3 (embedding, ~2 GB) and bge-reranker-v2-m3 (reranker, ~1 GB) into local cache

### Built-in Account

| Account | Password | Role |
|---------|----------|------|
| admin | `ChangeMe123!` | Super administrator (clearance 40, full permissions) |

Change this password immediately after logging in via "Admin → User Management".

### Demo Walkthrough

1. Log in as admin → go to the Admin panel → create a business knowledge base
2. Upload a PDF or Markdown document → wait for its status to become "Indexed"
3. Go back to the home page → select the knowledge base → ask a question → streaming answer with citations on the right
4. Switch to the "Public knowledge base" and ask again → compare permission differences

---

## 2. LAN Demo

For colleagues on the same Wi-Fi, access directly via your machine's LAN IP:

```
# Find your LAN IP (Windows PowerShell)
ipconfig | findstr "IPv4"
# Example output: 192.168.1.100

# Others open in their browser
http://192.168.1.100:5173
```

Allow the firewall ports (run once in an Administrator PowerShell):

```powershell
netsh advfirewall firewall add rule name="KAgent Web" dir=in action=allow protocol=TCP localport=5173
netsh advfirewall firewall add rule name="KAgent API" dir=in action=allow protocol=TCP localport=8000
```

Note: never expose the pg/redis/minio ports to the public internet.

---

## 3. Internet Demo (Zero Server Cost)

If your audience is not on the same LAN and you don't want to buy a cloud server — use a tunnel:

### Option A: cpolar (China-friendly, free tier is enough)

```bash
# 1. Download and install cpolar (https://www.cpolar.com/)
# 2. Expose local port 5173 with one command
cpolar http 5173

# 3. The output shows a public URL, e.g.:
#    Forwarding  http://xxxx.cpolar.io -> localhost:5173
# 4. Share http://xxxx.cpolar.io with your audience
```

### Option B: ngrok (international)

```bash
# 1. Download and install ngrok (https://ngrok.com/), register for a free token
# 2. Expose the port
ngrok http 5173

# 3. Share the Forwarding URL from the output
```

### Security Warning

Tunneling exposes your local services directly to the internet. **Make sure to do these three things**:

1. Change `SECRET_KEY` in `.env` (generate a new value with `openssl rand -hex 32`)
2. Use an LLM API key with no balance (prevent key abuse)
3. Stop the tunnel (Ctrl+C) immediately after the demo

---

## 4. Deploying to Alibaba Cloud

### Recommended Specs

| Scenario | ECS Instance | vCPU | RAM | Monthly Cost (approx.) |
|----------|--------------|------|-----|------------------------|
| Demo (local embedding + reranker) | `ecs.c7.share.xlarge` | 4 | 8 GB | ~200 CNY |
| Small team production | `ecs.c7.2xlarge` | 8 | 16 GB | ~600 CNY |
| Full production | `ecs.c7.4xlarge` | 16 | 32 GB | ~1200 CNY |

> If embedding/reranking are switched to external APIs (set `EMBEDDING_BASE_URL` / `RERANK_BASE_URL` in `.env`), RAM can be reduced to 4-8 GB.

### One-Command Deployment

```bash
# 1. Install Docker (Ubuntu 22.04)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker

# 2. Get the code
git clone https://gitee.com/zhang_-xiao/rag_-agent.git
cd rag_-agent/code

# 3. Edit .env (must change: SECRET_KEY, LLM_API_KEY, ADMIN_PASSWORD)
#    cp .env.prod.example .env  # production template
#    nano .env

# 4. Start
docker compose up -d --build

# 5. Wait for startup (first-time model download takes 5-30 minutes)
docker compose logs -f api

# 6. Open in browser
#    http://<server public IP>    (port 80 mapping)
```

See [code/docs/DEPLOY.md](code/docs/DEPLOY.md) for detailed production deployment instructions.

---

## 5. Architecture

### Container Topology

```
                    [External LLM API]
                         │
                    ┌────▼────┐
                    │  api     │  ── Python/FastAPI/uvicorn
                    │  :8000   │
                    └────┬────┘
           ┌─────────────┼─────────────┐
           │             │             │
      ┌────▼────┐  ┌─────▼────┐  ┌─────▼────┐
      │ postgres│  │  redis   │  │  minio   │
      │ pgvector│  │          │  │          │
      └─────────┘  └──────────┘  └──────────┘
           │             │
      ┌────▼─────────────▼────┐
      │      worker (arq)      │  ── async document parsing
      └───────────────────────┘
           │             │
      ┌────▼────┐  ┌─────▼────┐
      │embedding│  │ reranker │   ── local CPU inference
      │ bge-m3  │  │ bge-rer  │
      └─────────┘  └──────────┘

      ┌─────────┐
      │  web    │  ── React 18 + Nginx
      │  :5173  │
      └─────────┘
```

### Tech Stack

| Layer | Technology |
|-------|------------|
| Backend framework | FastAPI + Python 3.12 + asyncpg |
| Database | PostgreSQL 16 + pgvector 0.7 |
| Cache/Queue | Redis 7 + arq async tasks |
| Object storage | MinIO (S3-compatible) |
| Embedding model | bge-m3 (local CPU inference, 1024-dim) |
| Reranker model | bge-reranker-v2-m3 (local CPU inference) |
| LLM | OpenAI-compatible API (DeepSeek / Qwen, etc.) |
| Frontend | React 18 + TypeScript + Vite + Ant Design |
| Deployment | Docker Compose |

### Directory Layout

```
code/
├── apps/
│   ├── api/                     # Backend
│   │   ├── app/
│   │   │   ├── api/             # REST route layer (21 modules)
│   │   │   ├── services/        # Business logic (retrieval/generation/quota/sensitive words/feature flags...)
│   │   │   ├── models/          # SQLAlchemy ORM (PostgreSQL + pgvector)
│   │   │   ├── schemas/         # Pydantic request/response models
│   │   │   └── config/          # Configuration + global decision constants
│   │   ├── alembic/versions/    # 7 database migrations (0001~0007)
│   │   └── Dockerfile
│   └── web/                     # Frontend
│       ├── src/
│       │   ├── pages/           # ChatPage / AdminPage / KnowledgeBasesPage
│       │   ├── pages/admin/     # 8 admin panels (org/users/permissions/audit/quota...)
│       │   └── mocks/           # API client + type definitions
│       └── Dockerfile           # Multi-stage build: Vite → Nginx
├── docker-compose.yml           # Development environment (8 services)
├── docker-compose.prod.yml      # Production environment (4 services + 2 API workers)
├── .env.prod.example            # Production configuration template
└── docs/
    └── DEPLOY.md                # Production deployment guide (F1-F5 acceptance)
```

---

## 6. Feature List

| ID | Feature | Description |
|----|---------|-------------|
| F01 | Document upload | PDF / md / txt / xls / xlsx / docx, up to 100 MB per file, **no scanned-document OCR** |
| F02 | Automatic pipeline | Parse → chunk → vectorize → index, arq async jobs + progress polling |
| F03 | Hybrid retrieval | Vector similarity + ILIKE keywords → RRF fusion → bge-reranker reranking → 0.35 threshold gate |
| F04 | Answer generation | DeepSeek / Qwen SSE streaming, **with citation tracing** (click [n] to open the source snippet) |
| F05 | Answer boundaries | Answers only from retrieved knowledge; empty retrieval → refusal (three-stage grounding check) |
| F06 | Account login | RSA-encrypted passwords + JWT access/refresh tokens + Redis blacklist |
| F07 | Organization | Department tree (materialized path) + user groups + roles, full admin CRUD |
| F08 | KB membership | Four subject types (user/dept/group/role), add/remove/replace |
| F09 | Chunk-level ACL | Four-gate check (KB membership + clearance + deny tags + dept visibility), pushed down into retrieval SQL |
| F10 | Public KB | Single public knowledge base per tenant, visible to all logged-in users |
| F11 | Permission cache | Changes → immediate Redis invalidation + acl_tags recalculation |
| F12 | PII masking | Phone numbers / ID cards / bank cards auto-masked in answers |
| F13 | Audit logs | Who, when, what was asked, which documents were cited, refusal reasons — filterable by action/date |
| F14 | Multi-tenancy | tenants table isolation, full stack via Docker Compose |
| F15 | Data sync | Local directories + Git sources, scheduled incremental sync, manual trigger |
| F16 | Multi-turn chat | Conversation list + message history + last 3 turns of context + memory compression |
| F17 | Security hardening | Prompt-injection protection + Redis token-bucket rate limiting (20 req/min) + quota four-level degradation + bidirectional sensitive word filtering + department-based gray-scale feature flags |

### Admin Panels (visible to admin)

| Tab | Function |
|-----|----------|
| Organization | Department tree CRUD |
| Users | User CRUD + password reset |
| User Groups | Group CRUD + member management |
| Roles | Role CRUD + rename with auto-sync |
| Audit Logs | Action/date filters + details |
| Metrics | P50/P95/P99 latency + refusal rate |
| Eval Gate | Run eval cases, check answerable/refusal accuracy |
| Quota | User/tenant dual-layer token limits + usage progress bars |
| Sensitive Words | CRUD + batch import + block-on-hit |
| Feature Flags | Department-based flags + percentage rollout + instant rollback |

---

## 7. FAQ

### Q: embedding/reranker keeps being unhealthy after startup?

First startup downloads model weights (~1-2 GB each); check progress with `docker compose logs embedding`. If the network is slow, set `HF_ENDPOINT=https://hf-mirror.com` to speed up downloads.

### Q: pg_isready / redis errors after startup?

Passwords in `.env` only take effect on **first initialization**. To change a password: stop services → delete the `postgres_data` volume → restart.

### Q: A document stays in "Parsing" forever?

Check `docker compose logs worker`. Failed parsing jobs retry 3 times, then move to the dead letter queue. Common causes: corrupted file, unsupported format, MinIO unreachable.

### Q: Questions get refused with "No relevant content found"?

- Is the correct knowledge base selected?
- Has document parsing completed (status "Indexed")?
- Is your clearance level sufficient (high-clearance documents are invisible to low-clearance users)?
- Try adding keywords from the document to the question

### Q: Quota/sensitive words/feature flags don't seem to work?

M6 security features are disabled by default. Create rules in the "Feature Flags" tab and enable them:
- `quota / % / 100% / enabled` → enables quota management
- `sensitive_filter / % / 100% / enabled` → enables sensitive word filtering
- `rerank / % / 100% / enabled` → enables reranking

### Q: How do I change the admin password?

The admin password is read from the `ADMIN_PASSWORD` environment variable only when `seed_prod.py` first creates the account. Changing `.env` afterwards will not reset existing accounts. Reset it in "User Management" after logging in, or call `POST /api/users/{id}/reset-password`.

### Q: How do I temporarily stop everything?

```bash
docker compose down        # stop all services (data preserved)
docker compose up -d       # start again
```

Data volumes (postgres_data / minio_data / redis_data / hf_cache) live independently of containers — `down` does not lose data.

---

## 8. Upgrade & Rollback

Upgrade to a new version:

```bash
git pull
docker compose up -d --build api worker web
# alembic upgrade head runs automatically at startup
```

M6 rollback options (smallest impact first):

| Method | Operation | Impact |
|--------|-----------|--------|
| **Feature rollback (zero downtime)** | Disable the rule in the Feature Flags tab | Instant; takes effect when the 60s cache expires |
| **Migration rollback** | `docker compose exec api alembic downgrade -1` | Requires stopping api+worker |
| **Image rollback** | `git checkout <old commit>` → `docker compose up -d --build` | Reverts to a previous version |

See [code/docs/DEPLOY.md §9](code/docs/DEPLOY.md) for the full rollback decision tree.

---

## 9. Production Acceptance Checklist

| # | Operation | Expected |
|---|-----------|----------|
| F1 | Deploy on a clean machine per DEPLOY.md | Up within 30 minutes; login, Q&A, and audit all work |
| F2 | Create the public KB twice | Second attempt rejected by the unique index |
| F3 | Ask about a document containing phone/ID numbers | Masked in answers, unmasked in citations |
| F4 | Check audit logs | Shows who, when, what was asked, and which documents were cited |
| F5 | Backup + restore | Conversations, documents, and accounts fully intact after restore |
