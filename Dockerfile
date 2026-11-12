# One image for the harness and the sandbox: the same code, run as `astralvessel serve` or `astralvessel sandbox`.
# Agent packages are mounted at runtime; compose applies the sandbox hardening.
FROM python:3.15-slim
LABEL org.opencontainers.image.source="https://github.com/AstralDeep/AstralVessel"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ripgrep ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/astralvessel
COPY pyproject.toml README.md ./
COPY astralvessel ./astralvessel
COPY examples/field-guide ./examples/field-guide
COPY examples/sample-data ./examples/sample-data
ARG ASTRALPRIMS_VERSION=0.12.0
RUN pip install --no-cache-dir . "astralprims==${ASTRALPRIMS_VERSION}"

RUN useradd --create-home --uid 1001 --shell /usr/sbin/nologin vessel \
    && mkdir -p /data && chown vessel:vessel /data
USER vessel
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 VESSEL_HOST=0.0.0.0
EXPOSE 8700
CMD ["astralvessel", "serve"]
