FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY quietleaf_service ./quietleaf_service
RUN pip install --no-cache-dir .
COPY catalog ./catalog
ENV QUIETLEAF_CATALOG_DIR=/app/catalog
EXPOSE 8765
CMD ["python", "-m", "uvicorn", "quietleaf_service.app:app", "--host", "0.0.0.0", "--port", "8765"]
