FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
EXPOSE 3002
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "3002"]
