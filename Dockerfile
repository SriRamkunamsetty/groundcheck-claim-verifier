# ==============================================================================
# Production Multi-Stage Dockerfile for GroundCheck Claim Verifier
# Hardened, Non-Root, Minimal CVE Attack Surface
# ==============================================================================

# ------------------------------------------------------------------------------
# Stage 1: Build Environment
# ------------------------------------------------------------------------------
FROM python:3.11-slim AS builder

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Install build dependencies for C extensions / wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy package specifications
COPY pyproject.toml README.md /build/
COPY src /build/src

# Install package with all production extras
RUN pip install --upgrade pip setuptools wheel && \
    pip install "/build[all]"

# ------------------------------------------------------------------------------
# Stage 2: Production Runtime
# ------------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8000 \
    APP_HOME="/app"

WORKDIR ${APP_HOME}

# Install minimal runtime dependencies (curl for healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Security: Create unprivileged application user
RUN groupadd -g 10001 groundcheck && \
    useradd -u 10001 -g groundcheck -s /sbin/nologin -d ${APP_HOME} groundcheck

# Copy virtualenv and application artifacts from builder
COPY --from=builder /opt/venv /opt/venv
COPY --chown=groundcheck:groundcheck pyproject.toml README.md ${APP_HOME}/
COPY --chown=groundcheck:groundcheck src ${APP_HOME}/src
COPY --chown=groundcheck:groundcheck examples ${APP_HOME}/examples

# Drop root privileges
USER 10001:10001

EXPOSE 8000

# Container healthcheck using the native Kubernetes-compatible /healthz probe
HEALTHCHECK --interval=20s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -f http://127.0.0.1:${PORT}/healthz || exit 1

ENTRYPOINT ["groundcheck", "serve-api", "--host", "0.0.0.0", "--port", "8000"]
