FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY config.example.yaml .
RUN mkdir -p /app/data

EXPOSE 9527
CMD ["python", "-m", "app.main"]
