ARG BASE_IMAGE=nvcr.io/nvidia/pytorch:25.12-py3
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /tmp/
COPY install_system_deps.sh /tmp/install_system_deps.sh
COPY requirements.txt /tmp/requirements.txt
COPY requirements-vlm.txt /tmp/requirements-vlm.txt

COPY ./*.py /opt/mec-vlm/
COPY config.yaml /opt/mec-vlm/config.yaml

RUN ./install_system_deps.sh

WORKDIR /opt/mec-vlm/
RUN groupadd --gid 10001 app && \
    useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin app && \
    chown -R app:app /opt/mec-vlm

USER app
EXPOSE 8765

CMD ["/opt/mec-vlm-venv/bin/python", "/opt/mec-vlm/app.py", "--config", "/opt/mec-vlm/config.yaml"]
