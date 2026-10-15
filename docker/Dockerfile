# The control panel, built-in server and load generator in one container.
#   docker build -t datacenter-sim -f docker/Dockerfile .
#   docker run -p 8765:8765 -v dcsim-data:/data datacenter-sim
# Capacity follows the container's CPU and memory limits (--cpus, --memory).
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY dcsim ./dcsim
RUN pip install --no-cache-dir .
ENV DCSIM_HOME=/data \
    DCSIM__TARGET__HOST=0.0.0.0 \
    DCSIM__TARGET__PORT=8000 \
    DCSIM__TARGET__GRPC_PORT=50051 \
    DCSIM__CONTROL_PANEL__OPEN_BROWSER=false
EXPOSE 8765 8000 50051 5557
CMD ["dcsim", "--host", "0.0.0.0", "--port", "8765", "--no-browser"]
