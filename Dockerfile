# Imagen oficial de Playwright con Python 3.12 y navegadores Chromium preinstalados
FROM mcr.microsoft.com/playwright/python:v1.51.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Instalar dependencias Python
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Asegurar compatibilidad exacta del binario de Chromium con la versión de playwright instalada
RUN playwright install chromium

# Copiar código fuente
COPY . .

# Ejecutar como usuario no-root por seguridad en producción (Railway)
RUN useradd -m -u 10001 botuser && chown -R botuser:botuser /app
USER botuser

CMD ["python", "main.py"]
