FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY src/ ./src/

# Non-root user for security
RUN useradd -r -u 1001 mcpuser && chown -R mcpuser /app
USER mcpuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${MCP_PORT:-8000}/health')" || exit 1

CMD ["python", "src/server.py"]
