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
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 bot

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY . .

# Unprivileged, so code that compromises the browser does not run as root. The host's ./data
# must be owned by uid 1000.
USER bot
CMD ["python", "-m", "buysignal"]
