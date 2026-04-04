FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV QDRANT_URL=http://localhost:6333
ENV FASTMCP_HOST=0.0.0.0
ENV FASTMCP_PORT=8083
ENV MCP_API_KEY=""

EXPOSE 8083

CMD ["python", "main.py", "sse"]
