# The demo app in front of a Vessel, with AstralPrimitives and AstralProjection pinned to the commits AstralDeep uses.
# Build context is the repository root.
FROM python:3.11-slim

ARG ASTRALPRIMS_REF=d7b454105ec910cb1734fab6f7e318ac29185873
ARG ASTRALPROJECTION_REF=8fb733a6d17ad8bd18837180191d13a80b791fb8
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && pip install --no-cache-dir \
       "astralprims @ git+https://github.com/AstralDeep/AstralPrimitives@${ASTRALPRIMS_REF}" \
       "astralprojection @ git+https://github.com/AstralDeep/AstralProjection@${ASTRALPROJECTION_REF}" \
    && apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/astralvessel
COPY pyproject.toml README.md ./
COPY astralvessel ./astralvessel
RUN pip install --no-cache-dir .
COPY demo ./demo

RUN useradd --create-home --uid 1003 --shell /usr/sbin/nologin demo
USER demo
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8790
CMD ["uvicorn", "demo.app:app", "--host", "0.0.0.0", "--port", "8790", "--no-access-log"]
