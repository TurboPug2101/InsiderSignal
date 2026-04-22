FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (layer cached unless requirements change)
COPY worker/requirements.txt ./worker/requirements.txt
RUN pip install --no-cache-dir -r worker/requirements.txt

# Copy shared project modules the worker depends on
COPY db.py config.py ./
COPY scrapers/ ./scrapers/
COPY smart_money/ ./smart_money/
COPY services/ ./services/

# Copy worker source
COPY worker/ ./worker/

ENV PYTHONUNBUFFERED=1

EXPOSE 8001

CMD ["uvicorn", "worker.main:app", "--host", "0.0.0.0", "--port", "8001"]
