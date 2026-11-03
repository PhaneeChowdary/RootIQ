# payments-api image. Release 2026.10.03-1: smaller base image (commit 9c41e2a).
FROM alpine:3.20

WORKDIR /app
RUN apk add --no-cache python3 py3-pip

COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

COPY src/ ./src/

ENV GATEWAY_HOST=pay.gateway.example
ENV CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

CMD ["python3", "-m", "src.main"]
