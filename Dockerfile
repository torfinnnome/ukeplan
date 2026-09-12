FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN uv pip install --system -e .

EXPOSE 8000

ENV APP_HOST=0.0.0.0
ENV APP_PORT=8000

CMD ["python", "-m", "ukeplan"]
