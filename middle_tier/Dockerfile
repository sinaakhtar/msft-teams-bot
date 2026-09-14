# Bot Middle Tier - Cloud Run image.
#
# Contract this image honours:
#   * listens on $PORT (Cloud Run injects it; 8080 is only a local default)
#   * binds 0.0.0.0, not localhost
#   * runs as a non-root user
#   * contains NO secrets - the bot password and Entra client secret are read
#     from Secret Manager at startup, over the network, as the runtime service
#     account. Nothing sensitive is in a layer, an ENV, or a build arg.
#
# Two stages so the compiler toolchain that `cryptography`/`grpcio` may need
# does not ship to production.

# --------------------------------------------------------------------------
# Stage 1: build the virtualenv
# --------------------------------------------------------------------------
FROM python:3.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Install from the transitive lock file, not from the loose dependency ranges.
# A security boundary should be byte-identical between the image we tested and
# the image we deploy.
COPY requirements.lock.txt ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.lock.txt

# --------------------------------------------------------------------------
# Stage 2: runtime
# --------------------------------------------------------------------------
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8080

# Non-root. Cloud Run does not require it, which is exactly why it gets
# forgotten; a compromised relay should not also be root in its own namespace.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /srv
COPY --chown=app:app app ./app

USER 10001:10001

EXPOSE 8080

# No shell form, so the process is PID 1 and receives SIGTERM directly. Cloud
# Run sends SIGTERM on scale-in; a shell wrapper swallows it and the instance
# gets SIGKILLed 10 seconds later mid-request.
ENTRYPOINT ["python", "-m", "app.main"]
