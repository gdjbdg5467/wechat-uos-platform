# 使用 Python 3.11 作为基础镜像
FROM python:3.11-slim

# 安装依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 复制项目文件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py handlers.py tg_forward.py cftc.py lsposed.py main.py config.yaml ./

# 数据卷
VOLUME ["/data"]

# 暴露 QR 码 HTTP 端口
EXPOSE 8646

# 默认配置目录
ENV WXPOWERBOT_DATA_DIR=/data

CMD ["python3", "main.py"]
