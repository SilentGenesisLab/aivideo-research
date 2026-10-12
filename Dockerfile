FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    RUNS_DIR=/app/runs

WORKDIR /app
COPY requirements.txt .
# ffmpeg 由 imageio-ffmpeg 自带的静态版提供，不需要 apt 安装
RUN pip install -r requirements.txt

# 用与宿主机相同的 UID 运行，挂载出来的 runs/ 目录宿主机可直接读写
ARG UID=1000
RUN useradd -m -u ${UID} app && mkdir -p /app/runs && chown app /app/runs
COPY --chown=app . .
USER app

EXPOSE 8770
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8770/api/health', timeout=4)"
CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8770"]
