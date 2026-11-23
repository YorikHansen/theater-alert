FROM python:3.14-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install uv

COPY pyproject.toml ./
COPY src ./src

RUN uv sync --no-dev \
    && uv run playwright install --with-deps chromium

EXPOSE 8080

CMD ["uv", "run", "theater-alert", "run"]
