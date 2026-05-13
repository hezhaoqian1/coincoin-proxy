FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && npm install -g @slock-ai/daemon@0.47.0

COPY report_bot.py ./

ENV SLOCK_SERVER_URL=https://api.slock.ai
ENV SLOCK_REPORT_CHANNEL=#coincoin数据

CMD ["python3", "report_bot.py", "--send"]
