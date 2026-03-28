FROM node:22-slim
WORKDIR /app
RUN npm install -g @modelcontextprotocol/server-postgres supergateway
EXPOSE 3001
CMD ["supergateway", "--stdio", "mcp-server-postgres postgresql://openbrain:openbrain-db-2026@db:5432/openbrain", "--port", "3001", "--outputTransport", "streamableHttp", "--streamableHttpPath", "/", "--cors"]
