FROM willhallonline/ansible:2.16-debian-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-pip \
    && pip3 install --break-system-packages --no-cache-dir fastmcp>=2.0 pyyaml>=6.0 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY server.py .

EXPOSE 8000
CMD ["python3", "server.py"]
