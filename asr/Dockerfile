FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models/huggingface \
    XDG_CACHE_HOME=/models/cache

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        python3 \
        python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 asr \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin asr

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python3 -m pip install --break-system-packages --disable-pip-version-check -r /app/requirements.txt

COPY app.py /app/app.py

EXPOSE 9443
USER 10001:10001
CMD ["python3", "/app/app.py"]
