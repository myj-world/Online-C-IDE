FROM python:3.12-slim

# C compiler used by the Run button
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

# run as a non-root user (user programs are compiled and executed here)
RUN useradd -m appuser
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
USER appuser

# Render provides $PORT. One worker + many threads keeps all room state in one process.
CMD gunicorn -k gthread --threads 100 -w 1 --timeout 120 -b 0.0.0.0:${PORT:-10000} app:app
