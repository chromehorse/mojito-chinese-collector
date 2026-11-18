FROM python:3.12-slim-bookworm

WORKDIR /collector
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --only-binary=:all: -r requirements.txt
COPY _sources.json chinese_collector.py run.py ./
COPY scraper/ ./scraper/

USER 65532:65532
ENTRYPOINT ["python", "-B", "chinese_collector.py"]
