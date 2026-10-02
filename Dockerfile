FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install --no-cache-dir '.[live]' && mkdir -p /app/state && chown 10001:10001 /app/state
COPY config/example.json ./config/example.json
USER 10001:10001
ENTRYPOINT ["hip3-oracle"]
CMD ["--config", "/app/config/example.json", "run"]
