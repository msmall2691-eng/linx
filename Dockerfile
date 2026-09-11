# Single container: the frontend is built with Node, then served by the same
# FastAPI process that serves the API. One image, one port, one Railway service.

# ---- stage 1: build the frontend ----
FROM node:22-slim AS frontend

WORKDIR /build

# Copy manifests first so the dependency layer is cached independently of source.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
# vite.config.js writes to ../backend/static; inside this stage that resolves to
# /backend/static, which the next stage copies out.
RUN npm run build


# ---- stage 2: the runtime image ----
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./
COPY --from=frontend /backend/static ./static

# Run as a non-root user. Nothing in the image needs to write to it.
RUN useradd --create-home --shell /bin/bash linx \
    && chmod +x /app/entrypoint.sh \
    && chown -R linx:linx /app
USER linx

# Railway sets PORT; 8000 is the local default.
ENV PORT=8000
EXPOSE 8000

CMD ["/app/entrypoint.sh"]
