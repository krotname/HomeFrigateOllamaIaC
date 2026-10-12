FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f
LABEL org.opencontainers.image.source="https://github.com/krotname/HomeFrigateOllamaIaC"
LABEL org.opencontainers.image.go2rtc.revision="b5c61c18c26710d93c47e8efec9dc7ec14205c54"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN install -d -m 755 -o 1000 -g 1000 /app
USER 1000:1000
COPY --chown=1000:1000 wheels /tmp/wheels
RUN python -m pip install --no-compile --no-cache-dir --no-index --find-links=/tmp/wheels --target=/app/python cryptography==46.0.3 cffi==2.0.0 pycparser==2.23
ENV PYTHONPATH=/app/python
COPY --chown=1000:1000 go2rtc /app/go2rtc
COPY --chown=1000:1000 relay.py go2rtc.yml /app/
ENTRYPOINT ["/app/go2rtc", "-config", "/app/go2rtc.yml"]
