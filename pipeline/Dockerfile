# Single image, two processes (compose sets `command`):
#   receiver → uvicorn pipeline:app
#   worker   → arq pipeline.WorkerSettings
# git is included so backfill.py can walk a repo's history from inside the image.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Default to the receiver; compose overrides this for the worker.
CMD ["uvicorn", "pipeline:app", "--host", "0.0.0.0", "--port", "8000"]
