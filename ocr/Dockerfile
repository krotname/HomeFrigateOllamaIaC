FROM python:3.13-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# DejaVu Sans carries Cyrillic glyphs for the invisible text layer of searchable PDFs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 ocr \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin ocr

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python3 -m pip install --disable-pip-version-check -r /app/requirements.txt

COPY app.py /app/app.py

EXPOSE 443
USER 10001:10001
CMD ["python3", "/app/app.py"]
