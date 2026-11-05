FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
# Copiar el modelo ML directamente en la imagen
COPY data/dataset.pkl /app/data/dataset.pkl
ENV PYTHONPATH=/app
CMD ["sh", "-c", "uvicorn edgelab.api.app:app --host 0.0.0.0 --port 8080 & python -m edgelab.worker"]
