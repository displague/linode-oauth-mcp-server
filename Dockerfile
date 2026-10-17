FROM python:3.13-slim

WORKDIR /app
COPY . .
RUN pip install -e .

CMD ["linode-oauth-mcp-server"]
