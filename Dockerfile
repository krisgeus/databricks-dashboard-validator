# Image holding sqlfluff and the dashboard SQL validator.
#
# Everything the validator needs is a python dependency now, so the image is little more
# than a python base with the package installed: sqlfluff is pinned in pyproject.toml, and
# the extractor that used to be a jq program is part of the package.
FROM python:3.13-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Built from a scratch directory that is thrown away afterwards, so the image does not
# carry a pyproject.toml around at /. That matters: the validator treats the working
# directory's pyproject.toml as sqlfluff configuration when it has a [tool.sqlfluff]
# section, and its own must never be mistaken for the checked repository's.
COPY pyproject.toml README.md /build/
COPY src /build/src
RUN pip install --no-cache-dir /build && rm -rf /build

# Run as a normal user. pre-commit overrides this with the host uid so the scratch files
# it writes are not owned by root.
#
# pip puts the console script on the PATH at /usr/local/bin. Earlier images installed it at
# /bin, which is what the published hooks name as their entry point, so the old path is
# kept working.
RUN useradd --create-home --uid 1000 checker && \
    ln -s /usr/local/bin/validate-dashboard-sql /usr/bin/validate-dashboard-sql

# The test image: the same install, plus pytest and the tests themselves. The build
# pipeline runs `docker build --target test` so the unit tests exercise the very image
# that gets published.
FROM base AS test

RUN pip install --no-cache-dir pytest==9.1.1

WORKDIR /work
COPY tests /work/tests
COPY examples /work/examples
USER 1000
ENTRYPOINT []
# No cache provider: /work belongs to root and the tests run as uid 1000.
CMD ["pytest", "-q", "-p", "no:cacheprovider"]

# The published image. Last stage, so a plain `docker build` produces it.
FROM base AS runtime

USER 1000
ENTRYPOINT ["/bin/validate-dashboard-sql"]
