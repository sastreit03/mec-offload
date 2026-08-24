ARG BASE_IMAGE=oai-nr-ue-cuda:latest
FROM ${BASE_IMAGE}

USER root
WORKDIR /opt/ue-demo

COPY requirements.txt /opt/ue-demo/requirements.txt
COPY install_deps.sh /opt/ue-demo/install_deps.sh
RUN /opt/ue-demo/install_deps.sh

COPY . /opt/ue-demo/

EXPOSE 8080

# Keep your existing OAI entrypoint/startup behavior if the UE process is started
# elsewhere. This CMD only illustrates launching the demo application.
CMD ["/opt/ue-demo-venv/bin/python", "/opt/ue-demo/app.py", "--config", "/opt/ue-demo/config.yaml"]
