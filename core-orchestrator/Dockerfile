# Build context is the repository root (see docker-compose.yml), so the dashboard can be copied in.
FROM python:3.11-alpine

# iproute2 = tc, iperf3 = congestion flood, iputils = ping for RTT
RUN apk add --no-cache iproute2 iperf3 iputils

WORKDIR /srv
COPY core-orchestrator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY core-orchestrator/app ./app
COPY dashboard-ui /dashboard-ui
ENV DASHBOARD_DIR=/dashboard-ui

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
