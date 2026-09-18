# RAG Agent

## Introduction

RAG Agent is an enterprise-level Q&A system platform based on Retrieval-Augmented Generation (RAG). This project focuses on solving **data security** and **permission control** issues when deploying large models in enterprises. Through fine-grained Access Control Lists (ACL) and multi-tenant isolation mechanisms, it ensures data is "accessible but invisible."

The system provides complete knowledge base management, multi-format document parsing, streaming dialogue interaction, and backend administration features, supporting private deployment to meet extremely high compliance requirements.

## Core Features

*   **🛡️ Fine-grained Permission Control (ACL)**:
    *   Multi-dimensional permission system based on department, user group, and role.
    *   Documents and user settings support **security classification labels** (Public < Internal < Confidential < Top Secret).
    *   Inheritance logic: Subordinate departments inherit permissions from superior levels by default, supporting reverse visibility control.
*   **🔍 Hybrid Search & Ranking**:
    *   Supports fusion of vector search and keyword search (RRF algorithm).
    *   Integrated Rerank (re-ranking) model to improve search accuracy.
*   **📄 Multi-format Document Parsing**:
    *   Native support for PDF, DOCX, Markdown, Excel (XLS/XLSX), plain text, and other formats.
    *   Automatic semantic chunking, preserving contextual associations.
*   **💬 Intelligent Chat Experience**:
    *   Streaming output (SSE), real-time response.
    *   Source citation: Generated answers can be precisely traced back to original document fragments.
    *   Sensitive word filtering and masking to protect privacy information (PII Masking).
*   **⚙️ Powerful Admin Backend**:
    *   User and organizational structure management.
    *   Knowledge base member and quota management.
    *   Audit logs record all critical operations.
    *   Feature Flags and System Metrics.

## Tech Stack

*   **Backend**: Python 3.12, FastAPI, SQLAlchemy (Async), Alembic
*   **Frontend**: React 18, TypeScript, Ant Design, Vite
*   **Database**: PostgreSQL (Main Data), Redis (Cache/Session/Rate Limiting)
*   **Infrastructure**: Docker, Docker Compose

## Quick Start

### Environment Preparation

*   Python >= 3.12
*   Node.js >= 18
*   PostgreSQL >= 14
*   Redis >= 6.2

### 1. Clone & Install Dependencies

```bash
# Clone repository
git clone https://gitee.com/zhang_-xiao/rag_-agent.git
cd rag_-agent/code

# Install backend dependencies
cd apps/api
python -m pip install -r pyproject.txt

# Install frontend dependencies
cd ../web
npm install
```

### 2. Configure Environment Variables

Backend configuration is located in the `apps/api` directory.

```bash
# Copy example configuration
cp apps/api/.env.example apps/api/.env
# Edit .env file, configure database connection, Redis, LLM API Key, etc.
```

### 3. Initialize Database

Ensure PostgreSQL and Redis are running.

```bash
cd apps/api

# Execute database migration
alembic upgrade head

# Initialize basic data (roles, departments, superusers, etc.)
python -m scripts.seed
```

### 4. Start Services

**Start Backend API:**

```bash
cd apps/api
uvicorn app.main:app --reload --port 8000
```

**Start Frontend Development Server:**

```bash
cd apps/web
npm run dev
```

Visit `http://localhost:5173` (default port) to begin using.

## Project Structure

```
rag_-agent/
├── apps/
│   ├── api/                 # FastAPI backend service
│   │   ├── app/
│   │   │   ├── api/         # Route interface definitions
│   │   │   ├── models/      # Database entities (SQLAlchemy Models)
│   │   │   ├── schemas/     # Pydantic data models
│   │   │   ├── services/    # Core business logic (ACL, RAG, LLM, Sync)
│   │   │   ├── workers/     # Async task Workers
│   │   │   └── config/      # Configuration management
│   │   ├── alembic/         # Database migration scripts
│   │   ├── scripts/         # Ops scripts (seed, eval)
│   │   └── tests/           # Unit tests and ACL strategy tests
│   │
│   └── web/                 # React frontend application
│       ├── src/
│       │   ├── pages/       # Page views (Chat, Admin, KB)
│       │   ├── components/  # Common components
│       │   ├── mocks/       # Data mocking
│       │   └── store/       # State management
│       └── ...
│
├── docs/                    # Documentation
│   └── DEPLOY.md            # Production environment deployment guide
├── scripts/                 # Root directory tool scripts
└── docker-compose.yml       # Development environment orchestration
```

## Detailed Function Modules

### 1. Permission System (ACL)

This is the core differentiator of the system, located in `app/services/acl/`.
*   **Visibility Judgment**: When users ask questions or search, the system compares their `clearance` (clearance level) with the document/department `level` (classification) to reject data returns below the required security level.
*   **Isolation**: Implements tenant (Tenant) isolation, group visibility, and department inheritance logic.

### 2. Knowledge Base & Sync

*   Supports manual document upload, and supports configuring `SyncSource` to automatically pull document updates from Git repositories.
*   The document parsing service is in `app/services/parse/`, supporting preprocessing for multiple formats.

### 3. RAG Flow

1.  **Retrieve**: `app/services/retrieve/` - Hybrid search (Vector + Keyword).
2.  **Rerank**: `app/services/rerank/` - Precision ranking of search results.
3.  **Generate**: `app/services/generate/` - Build Prompt to call LLM, and build Citations.
4.  **Grounding**: `app/services/grounding/` - Verify if LLM responses are based on the provided context.

### 4. Evaluation (Evaluation)

The system includes built-in evaluation scripts located at `scripts/run_eval.py` and `app/services/eval_service.py`, used to verify the accuracy and refusal rate of the RAG flow.

## Deployment Guide

Please refer to `docs/DEPLOY.md` for detailed production environment deployment, backup recovery, and maintenance commands.

## Testing

The project includes deep unit tests for ACL permission logic to ensure the accuracy of permission control.

```bash
cd apps/api
pytest tests/acl/ -v
```

## License

This project is open-sourced on Gitee. Please refer to the LICENSE file in the project root directory for the specific license agreement.