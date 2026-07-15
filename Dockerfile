FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p static/outputs
ENV PORT=10000
EXPOSE 10000
# IMPORTANT: --workers 1. The job state is held in process memory (jobs dict).
# Multiple workers each have their own copy and a job started in worker A
# will be reported as "Job not found" by worker B. Single worker is required
# for the status endpoint to work. Threads let us run the long scraper
# without blocking Flask's request loop.
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 900 --graceful-timeout 60
