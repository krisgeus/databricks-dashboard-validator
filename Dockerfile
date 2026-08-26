# Image holding sqlfluff, jq and the dashboard SQL extractor.
FROM python:3.13-slim-bookworm

ARG SQLFLUFF_VERSION=4.3.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# hadolint ignore=DL3008
# jq is pulled from debian stable; pinning the patch version here breaks the build every
# time debian ships a security update for it.
RUN apt-get update && \
    apt-get install -y --no-install-recommends jq && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "sqlfluff==${SQLFLUFF_VERSION}"

COPY validate-dashboard-sql.sh /bin/validate-dashboard-sql
COPY extract-sql-snippets.jq /bin/extract-sql-snippets.jq
COPY sqlfluff-defaults.cfg /bin/sqlfluff-defaults.cfg
COPY sqlfluff-expressions.cfg /bin/sqlfluff-expressions.cfg

# Run as a normal user. pre-commit overrides this with the host uid so the scratch files
# it writes are not owned by root.
RUN useradd --create-home --uid 1000 checker
USER 1000

ENTRYPOINT ["/bin/validate-dashboard-sql"]
