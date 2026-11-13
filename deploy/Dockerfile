FROM python:3.12-slim-bookworm
WORKDIR /app
COPY pyproject.toml README.md MANIFEST.in ./
COPY src ./src
RUN python -m pip install --no-cache-dir '.[research,llm,assets]'
EXPOSE 18080
CMD ["news2douyin-server", "--host", "0.0.0.0", "--port", "18080", "--storage-root", "/data"]
