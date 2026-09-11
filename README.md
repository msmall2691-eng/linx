# linx

A two-sided marketplace connecting short-term-rental property owners with
independent cleaners for turnover cleanings.

Owners post a turnover between a guest checkout and the next checkin. Vetted
local cleaners bid a price. The owner awards the job. One category, one region
at launch, a standing bid-and-award relationship rather than one-off lead sales.

**This repository is standalone.** It has no dependency on, import from, or
reference to any other codebase.

---

## Status: phase 1 — scaffolding, auth, and schema

What works today:

- Signup and login for owners and cleaners, JWT-based, with a role gate
  (`owner` / `cleaner` / `admin`) enforced server-side.
- The full v1 database schema — all ten tables — created by one Alembic
  migration. Tables belonging to later phases exist and are empty on purpose.
- A single-container deploy: the backend serves the built frontend.
- 62 tests, running against real PostgreSQL.

Bidding, awarding, payments, notifications, and reviews are **not** built yet.
Each is its own phase, reviewed before the next begins — see the phase table in
[`CLAUDE.md`](CLAUDE.md).

---

## Read CLAUDE.md first

[`CLAUDE.md`](CLAUDE.md) holds three guardrails that constrain every later
phase. In short:

1. **Row-lock before checking, on anything that awards work.**
   `SELECT ... FOR UPDATE` on the `Turnover` row *before* reading its state, with
   the check and the write in one transaction. Two simultaneous accepts on one
   turnover must not both succeed.
2. **Idempotency keys derived from stable database ids**, never freshly
   generated — a retry has to produce the *same* key. Write the attempt to the
   row *before* the network call, so a crash leaves a row flagged for review
   rather than one that looks untouched.
3. **Before removing a step from a flow, find out what depends on it** — and
   write down what you found.

---

## Stack

| Layer | Choice |
|---|---|
| Backend | FastAPI, SQLAlchemy 2.0, Alembic |
| Database | PostgreSQL |
| Frontend | React 18, Vite, Tailwind CSS |
| Auth | JWT, role-gated |
| Deploy | One Docker image on Railway, with its own Postgres |

---

## Local development

### Prerequisites

PostgreSQL 14+, Python 3.11+, Node 20+.

```bash
createdb linx_dev
createdb linx_test
```

### Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

cp ../.env.example .env        # then edit DATABASE_URL if yours differs
alembic upgrade head
uvicorn app.main:app --reload  # http://localhost:8000
```

Interactive API docs: <http://localhost:8000/docs>

### Frontend

```bash
cd frontend
npm install
npm run dev                    # http://localhost:5173
```

The dev server proxies `/api` to `localhost:8000`, so the app uses the same
relative paths in development and production. There is no environment-specific
base URL anywhere in the frontend code.

### Tests

```bash
cd backend
TEST_DATABASE_URL=postgresql+psycopg://linx:linx@localhost:5432/linx_test pytest
```

**The suite requires PostgreSQL and will not run on SQLite.** Guardrail 1
depends on `SELECT ... FOR UPDATE`, which SQLite does not implement — a suite on
SQLite would pass while the double-award race stayed open in production. The
suite also builds its schema by running the real migrations, not
`create_all`, so a migration that drifts from the models fails in tests instead
of at deploy.

---

## Deploying

One Railway service, one Postgres, nothing shared with any other project.

1. Create a new Railway project and add a **PostgreSQL** database.
2. Point a service at this repo. `railway.json` selects the Dockerfile build and
   health-checks `/api/health`.
3. Set the environment variables:

   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | Railway's Postgres URL (`${{Postgres.DATABASE_URL}}`) |
   | `SECRET_KEY` | `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
   | `ENVIRONMENT` | `production` |
   | `CORS_ORIGINS` | your deployed origin |
   | `REGION_NAME` | the pilot region |

   Startup refuses to run with the development `SECRET_KEY` when
   `ENVIRONMENT=production`.

4. Deploy. The entrypoint runs `alembic upgrade head` before starting the
   server, and aborts the boot if a migration fails rather than serving traffic
   against a schema it does not match.

The image builds the frontend with Node, then copies the static build into the
Python runtime image — `PORT` is read from the environment, so Railway's port
assignment works unmodified.

---

## Layout

```
CLAUDE.md              the three guardrails and the domain rules — read first
Dockerfile             two-stage build, single runtime image
railway.json           Railway build and health-check config
backend/
  app/
    main.py            FastAPI app; serves /api and the built frontend
    config.py          settings, from the environment
    db.py              engine and session
    core/security.py   password hashing and JWT
    api/deps.py        current user and the role gate
    api/routes/        auth, health
    models/            SQLAlchemy models, one file per table
    schemas/           Pydantic request/response shapes
  alembic/versions/    migrations — one head, always
  tests/               pytest suite, real Postgres
frontend/
  src/
    lib/api.js         the one place that knows about tokens and errors
    lib/auth.jsx       session context
    components/        nav, route guard
    pages/             landing, login, signup, dashboard
```

---

## Stripe

All Stripe work happens in **test mode** until the go-live gate. Test mode is
sandboxed — no EIN, no SSN, no real money — so the integration can be built and
tested in full before the entity exists.

The line that must not be crossed: **no real pilot transactions in live mode
under a personal SSN or an existing company's EIN.** Going live means a new,
separate Connect platform account under the new entity, not a migrated one.
