FROM ghcr.io/home-assistant/base:latest

RUN apk add --no-cache python3 py3-aiohttp

COPY rootfs /
WORKDIR /app
CMD [ "python3", "-u", "/app/main.py" ]
