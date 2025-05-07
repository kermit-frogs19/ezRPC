FROM python:3.12

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1


# Install OS dependencies (openssl needed for certs, net-tools for testing)
RUN apt-get update && apt-get install -y \
    build-essential \
    libssl-dev \
    net-tools \
    iputils-ping \
    certbot \
    && rm -rf /var/lib/apt/lists/*

# Set work directory
WORKDIR /app


# Copy requirements
COPY requirements.txt .

# Install Python deps
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY . .


# Expose UDP port used by QUIC
EXPOSE 8080/udp

# Run your QUIC server
CMD ["python", "main.py"]