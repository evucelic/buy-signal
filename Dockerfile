FROM python:3.11-slim

# Unbuffered stdout so `docker compose logs` shows output in real time, not just on exit/flush.
ENV PYTHONUNBUFFERED=1

# chromium/chromium-driver: collectors/fed_rate.py's Selenium scraper needs a real browser.
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium chromium-driver \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
