FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 TZ=Europe/Madrid EDGELAB_DB=/app/data/edgelab.sqlite HOME=/app EDGELAB_LIGHT=1 MALLOC_ARENA_MAX=2
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt huggingface_hub
COPY edgelab ./edgelab
COPY web ./web
COPY deploy/render/start.sh ./start.sh
RUN mkdir -p /app/data && chmod +x start.sh
EXPOSE 10000
CMD ["./start.sh"]
