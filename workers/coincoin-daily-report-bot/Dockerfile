FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && npm install -g @slock-ai/daemon@0.47.0 \
    && printf '#!/bin/sh\nexec node /usr/local/lib/node_modules/@slock-ai/daemon/dist/cli/index.js "$@"\n' > /usr/local/bin/slock \
    && chmod +x /usr/local/bin/slock \
    && slock --help >/dev/null

COPY report_bot.py ./

ENV SLOCK_SERVER_URL=https://api.slock.ai
ENV SLOCK_REPORT_CHANNEL=#coincoin数据

CMD ["python3", "report_bot.py", "--send"]
