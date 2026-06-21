FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/       ./app/
COPY static/    ./static/
COPY migrations/ ./migrations/

# Non-root user for safety
RUN useradd -m tracker && chown -R tracker /app
USER tracker

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
