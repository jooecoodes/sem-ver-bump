# syntax=docker/dockerfile:1

FROM python:3.12-slim AS dev

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git make ca-certificates binutils \
    && rm -rf /var/lib/apt/lists/*

ARG UID=1000
ARG GID=1000
RUN groupadd --gid "${GID}" dev \
    && useradd --create-home --uid "${UID}" --gid "${GID}" dev

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e '.[dev]'

USER dev
CMD ["svb", "--help"]
