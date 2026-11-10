# One image for the harness and the sandbox: the same code, run as `astralvessel serve` or `astralvessel sandbox`.
# Agent packages are mounted at runtime; compose applies the sandbox hardening.
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ripgrep ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/astralvessel
COPY pyproject.toml README.md ./
COPY astralvessel ./astralvessel
ARG ASTRALPRIMS_REF=d7b454105ec910cb1734fab6f7e318ac29185873
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && pip install --no-cache-dir . "astralprims @ git+https://github.com/AstralDeep/AstralPrimitives@${ASTRALPRIMS_REF}" \
    && apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1001 --shell /usr/sbin/nologin vessel \
    && mkdir -p /data && chown vessel:vessel /data
USER vessel
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 VESSEL_HOST=0.0.0.0
EXPOSE 8700
CMD ["astralvessel", "serve"]
