FROM python:3.12-slim-bookworm

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts

ARG PYTORCH_INDEX_URL=https://download.pytorch.org/whl/cpu

RUN pip install --no-cache-dir --index-url "${PYTORCH_INDEX_URL}" torch \
    && pip install --no-cache-dir . \
    && groupadd --gid 10001 knowledge \
    && useradd --uid 10001 --gid knowledge --create-home --no-log-init knowledge \
    && mkdir -p /app/knowledge /home/knowledge/.cache/huggingface \
    && chmod -R a+rX /app/src /app/scripts \
    && chown -R knowledge:knowledge /app/knowledge /home/knowledge

USER knowledge

EXPOSE 8000

CMD ["knowledge-mcp"]
