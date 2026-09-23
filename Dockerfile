# autoposter runtime: python + playwright's firefox, nothing else.
# The CODE is not baked in — compose bind-mounts the repo at /app, so code,
# data/post.txt and the photo folders can be edited on the host and are
# picked up immediately. Rebuild ONLY when dependencies change:
#   docker compose build
#
# The persistent Firefox profile (logins/cookies) lives in the bind-mounted
# .local-capture/profiles/facebook, so a one-time manual login inside a
# container survives into every later container run.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# fonts only: a sane set with Latin-1 accented glyphs (the FB UI we drive is
# Spanish). Everything else firefox needs is installed by playwright below.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl procps \
        fontconfig fonts-liberation fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

RUN pip install "playwright>=1.48,<2" \
    && playwright install --with-deps firefox

WORKDIR /app
ENV PYTHONPATH=/app:/app/scraping_recorder

# default command = the dashboard; `docker compose run --rm autoposter
# python -m poster.main --dry-run` (etc.) overrides it per invocation
CMD ["python", "-m", "poster.gui", "--port", "8765"]
