FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code (fingerprint subsystem is Windows-only; on Linux the
# chat API + dashboard still run, fingerprint features degrade gracefully)
COPY *.py ./
COPY templates/ templates/
COPY assets/ assets/
COPY .env.example .env.example

# Runtime artifacts (cookies cache, logs) live in /app/secrets and /app/logs
RUN mkdir -p secrets logs

EXPOSE 4444 4445 4446

CMD ["python", "gemini_core.py"]
