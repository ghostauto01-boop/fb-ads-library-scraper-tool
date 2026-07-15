FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p static/outputs
ENV PORT=10000
EXPOSE 10000
# Single worker, multi-threaded. The scraper is run inside an SSE
# handler thread, so the Flask loop stays free to serve health checks
# and the index page.
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 0 --keep-alive 75 --graceful-timeout 60
