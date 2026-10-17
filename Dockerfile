FROM python:3.13-slim

WORKDIR /app
COPY . .
RUN pip install -e .

CMD ["linode-rfc9727-mcp-server"]
