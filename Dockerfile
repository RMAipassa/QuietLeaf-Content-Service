FROM python:3.12-slim
WORKDIR /app
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
COPY pyproject.toml ./
COPY quietleaf_service ./quietleaf_service
RUN pip install --no-cache-dir . \
    && python -m playwright install --with-deps chromium
COPY catalog ./catalog
ENV QUIETLEAF_CATALOG_DIR=/app/catalog
EXPOSE 8765
CMD ["python", "-m", "uvicorn", "quietleaf_service.app:app", "--host", "0.0.0.0", "--port", "8765"]
