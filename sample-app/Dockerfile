FROM python:3.12-slim

WORKDIR /app

# Install dependencies as root
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create non-root user
RUN useradd -m appuser

# Copy app files with correct ownership
COPY --chown=appuser:appuser app.py .

USER appuser

EXPOSE 8080

CMD ["gunicorn", "app:app", \
     "--bind", "0.0.0.0:8080", \
     "--workers", "4", \
     "--threads", "2", \
     "--timeout", "30", \
     "--access-logfile", "-"]
