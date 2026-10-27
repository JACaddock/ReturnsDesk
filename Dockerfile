# Builds the React UI, then serves it from the API on :8000.
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY returns_desk returns_desk
# Editable, so config.ROOT stays /app and the API finds web/dist and data/.
RUN pip install --no-cache-dir -e .
COPY --from=web /web/dist web/dist
EXPOSE 8000
# Seeds on first start only, so refunds and returns made in the demo survive a restart.
CMD ["sh", "-c", "test -f data/returns_desk.db || python -m returns_desk.seed; exec uvicorn returns_desk.api:create_app --factory --host 0.0.0.0 --port 8000"]
