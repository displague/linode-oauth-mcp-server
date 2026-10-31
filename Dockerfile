FROM python:3.15-slim

WORKDIR /app
COPY . .
RUN pip install -e .

CMD ["linode-oauth-mcp-server"]
