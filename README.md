# linx

A two-sided marketplace connecting short-term-rental property owners with
independent cleaners for turnover cleanings.

Owners post a turnover between a guest checkout and the next checkin. Vetted
local cleaners bid a price. The owner awards the job. One category, one region
at launch, a standing bid-and-award relationship rather than one-off lead sales.

**This repository is standalone.** It has no dependency on, import from, or
reference to any other codebase.

---

## Status: phase 3 — the cleaner's side

What works today:

- Signup and login for owners and cleaners, JWT-based, with a role gate
  (`owner` / `cleaner` / `admin`) enforced server-side.
- **Properties** — owner-scoped create, list, detail, edit, and archive.
  Archiving is refused while turnovers are still on the schedule.
- **Turnovers** — post one against a property with a checkout and an optional
  next checkin, keep it as a draft or put it on the bench, reschedule it, cancel
  it. The **urgency ladder** is derived server-side from how close checkout is
  to the next checkin, in region-local time.
- **Cleaner profiles** — service area as a point and a radius, vetting document
  uploads (ID / insurance / reference), and a background-check step.
- **An admin vetting queue** — a human reviews a photo ID and a reference and
  records the background-check result. Nothing here can override the bidding
  gate: `can_take_jobs` is computed by Postgres from the two statuses.
- **The bench board and bidding** — open turnovers inside a cleaner's radius,
  most urgent first, with the street address and access notes withheld until a
  job is awarded. A cleared cleaner names a price.
- The full v1 database schema — all ten tables — created by one Alembic
  migration. Tables belonging to later phases exist and are empty on purpose.
- A single-container deploy: the backend serves the built frontend.
- 209 tests against real PostgreSQL, plus 5 browser click-throughs.

Awarding, payments, notifications, and reviews are **not** built yet.
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

### Browser tests

`backend/tests/e2e/` drives the real app in a real browser. They catch the bug
class endpoint tests cannot see: an action that returns 200 while the screen it
belongs to goes blank. They skip themselves unless you opt in:

```bash
cd frontend && npm run build          # the backend serves this build
cd ../backend
pip install -r requirements-e2e.txt
playwright install chromium
LINX_E2E=1 pytest tests/e2e
```

They run as their own CI job, so a dead screen fails the build.

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
   | `REGION_TIMEZONE` | its IANA zone, e.g. `America/New_York` |
   | `DOCUMENT_STORAGE_DIR` | a path on a **mounted volume** — see below |

   **Vetting documents need a Railway volume.** Mount one and point
   `DOCUMENT_STORAGE_DIR` at it. Without a volume the container filesystem is
   replaced on every deploy and uploaded IDs disappear while their database rows
   survive, so the admin queue ends up pointing at files that are gone.

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
    services/urgency.py  the one place that decides the urgency ladder
    services/turnovers.py  the only writes to the derived columns
    services/vetting.py  the one place that says why a cleaner can or cannot bid
    services/geo.py      service-radius distance, in Python and in SQL
    services/storage.py  vetting documents, never on a public path
    services/background_check.py  Checkr, or manual when no key is set
  alembic/versions/    migrations — one head, always
  tests/               pytest suite, real Postgres
  tests/e2e/           browser click-throughs, opt-in
frontend/
  src/
    lib/api.js         the one place that knows about tokens and errors
    lib/auth.jsx       session context
    lib/config.jsx     the region's name and timezone, from the server
    lib/datetime.js    region-local time conversion; money in integer cents
    components/        nav, route guard, badges, forms
    pages/             landing, auth, dashboard, properties, turnovers
```

---

## Stripe

All Stripe work happens in **test mode** until the go-live gate. Test mode is
sandboxed — no EIN, no SSN, no real money — so the integration can be built and
tested in full before the entity exists.

The line that must not be crossed: **no real pilot transactions in live mode
under a personal SSN or an existing company's EIN.** Going live means a new,
separate Connect platform account under the new entity, not a migrated one.
