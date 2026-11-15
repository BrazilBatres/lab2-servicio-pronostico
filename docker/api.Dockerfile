# Imagen de la API y del servicio bootstrap (mismo código, distinto comando)
FROM python:3.12.8-slim
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ /app/src/
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "novamercado.api:app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8000"]
