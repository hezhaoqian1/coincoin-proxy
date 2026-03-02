# Stage 1: Build React frontend
FROM node:20-slim AS frontend
WORKDIR /web
COPY coincoin-web/package*.json ./
RUN npm ci --production=false
COPY coincoin-web/ ./
RUN npm run build

# Stage 2: Python backend + static frontend
FROM python:3.11-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY static/ ./static/
COPY --from=frontend /web/dist ./static/web

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
