# Traffic API (and the fake traffic generator, which is stdlib only).
# Voice and the dashboard bridge stay on the host: containers on macOS can't reach the mic or speaker.
FROM python:3.11-slim

WORKDIR /app
RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn[standard]>=0.29"

COPY eddie/ eddie/
COPY scripts/fake_traffic.py scripts/

EXPOSE 8000
CMD ["uvicorn", "eddie.api:app", "--host", "0.0.0.0", "--port", "8000"]
