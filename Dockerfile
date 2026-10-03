FROM python:3.12-slim

# WITH_LAYA=1 adds the optional local Laya judge (CPU torch, ~2.5 GB RAM while loaded). Default: Jev API / none.
ARG WITH_LAYA=0

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    if [ "$WITH_LAYA" = "1" ]; then \
      pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu && \
      pip install --no-cache-dir "laya>=0.3.3"; \
    fi

# Bake the embedding + rerank models into the image so startup needs no network.
ENV HF_HOME=/models FASTEMBED_CACHE_PATH=/models
RUN python -c "from fastembed import TextEmbedding; from fastembed.rerank.cross_encoder import TextCrossEncoder; \
TextEmbedding('BAAI/bge-small-en-v1.5'); TextCrossEncoder('Xenova/ms-marco-MiniLM-L-6-v2')"

COPY server/ server/

ENV DB_PATH=/data/memory.db HOST=0.0.0.0 PORT=8084 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8084
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health', timeout=4)"

CMD ["python", "server/main.py"]
