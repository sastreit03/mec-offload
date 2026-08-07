FROM oai-nr-ue-cuda:latest

USER root

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3-pip \
        python3-venv && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt

RUN python3 -m venv /opt/ueclientvenv && \
    /opt/ueclientvenv/bin/python -m pip install --no-cache-dir \
        -r /tmp/requirements.txt && \
    rm -f /tmp/requirements.txt