FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV DB_PATH=/data/timing.db \
    STATION_COUNT=6

EXPOSE 8000

# 启动时自动建表；数据库为空时写入示例数据
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
