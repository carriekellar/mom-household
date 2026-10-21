FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATABASE_PATH=/data/household.sqlite3
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && mkdir -p /data
COPY household.py language.py transport.py server.py ./
EXPOSE 8000
CMD ["python", "server.py"]
