FROM python:3.12.7-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY infra/docker/python-entrypoint.sh /usr/local/bin/wikipediarag-python-entrypoint
RUN chmod +x /usr/local/bin/wikipediarag-python-entrypoint
RUN pip install --no-cache-dir .

EXPOSE 8080 8000
ENTRYPOINT ["wikipediarag-python-entrypoint"]
