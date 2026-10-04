FROM python:3.11-slim

WORKDIR /app

# Prevent Python from writing .pyc files and enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Install Python dependencies (using PyTorch CPU wheels to keep image lightweight and fast)
COPY requirements.txt .
RUN pip install --no-cache-dir --default-timeout=1000 --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.txt

# Copy Alembic migration configuration and domain source code
COPY alembic.ini .
COPY alembic alembic
COPY pyproject.toml .
COPY cadence cadence

# Install Cadence package
RUN pip install --no-cache-dir -e .

# Copy and configure entrypoint script
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN sed -i 's/\r$//' /app/docker-entrypoint.sh && chmod +x /app/docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "cadence.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
