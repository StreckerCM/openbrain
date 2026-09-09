FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt
COPY server.py auth.py db.py embeddings.py .
EXPOSE 3001 3002
CMD ["python", "server.py"]
