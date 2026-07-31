FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install the package; dependencies resolve from pyproject.toml
COPY pyproject.toml README.md LICENSE ./
COPY ezRPC ./ezRPC
RUN pip install --no-cache-dir .

COPY main.py example.py ./

# Bind on all interfaces inside the container; QUIC runs over UDP
ENV EZRPC_HOST=0.0.0.0 \
    EZRPC_PORT=8000
EXPOSE 8000/udp

CMD ["python", "main.py"]
