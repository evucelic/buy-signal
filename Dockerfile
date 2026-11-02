FROM python:3.11-slim

# Unbuffered stdout so `docker compose logs` shows output in real time, not just on exit/flush.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    MPLCONFIGDIR=/tmp/buysignal-mpl \
    XDG_CACHE_HOME=/tmp/buysignal-cache \
    MPLBACKEND=Agg

# chromium/chromium-driver: the Selenium scrapers (buysignal/collectors/chrome.py) need a real browser.
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium chromium-driver \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "-m", "buysignal"]
