FROM python:3.12-slim

# C compiler used by the Run button (installed as root)
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

# Hugging Face Spaces runs containers as UID 1000
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PORT=7860

WORKDIR /home/user/app
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt
COPY --chown=user app.py .

EXPOSE 7860
# One worker + many threads keeps live room state in a single process
CMD gunicorn -k gthread --threads 100 -w 1 --timeout 120 -b 0.0.0.0:${PORT} app:app
