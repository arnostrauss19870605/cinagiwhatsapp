# ---------- stage 1: build the CSS ----------
FROM node:20-alpine AS css
WORKDIR /build
COPY package.json ./
RUN npm install --no-audit --no-fund
COPY theme/static_src/input.css ./theme/static_src/input.css
COPY templates ./templates
COPY apps ./apps
RUN npx @tailwindcss/cli -i ./theme/static_src/input.css -o ./app.css --minify

# ---------- stage 2: the app ----------
FROM python:3.13-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential libpq-dev curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --uid 1000 --create-home app
WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy with ownership set - never `chown -R` afterwards, it costs ~100s a deploy.
COPY --chown=app:app . /app
COPY --from=css --chown=app:app /build/app.css /app/static/css/app.css

# Volume mount points must exist in the image and be owned by the app user.
# Docker seeds an empty named volume from the image path; a path that is not
# in the image produces a root-owned volume that uid 1000 cannot write to.
RUN mkdir -p /app/staticfiles /app/media \
    && chown app:app /app/staticfiles /app/media

USER app
EXPOSE 8000

# Overridden by compose for the worker and beat services.
CMD ["gunicorn", "config.asgi:application", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "60"]
