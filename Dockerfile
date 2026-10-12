FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY p2p/ p2p/

ENTRYPOINT ["python", "-m", "p2p"]
