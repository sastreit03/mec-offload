ARG BASE_IMAGE=oai-nr-ue-cuda:latest
FROM ${BASE_IMAGE}

USER root
WORKDIR /opt/ue-demo

COPY requirements.txt /opt/ue-demo/requirements.txt
COPY install_deps.sh /opt/ue-demo/install_deps.sh
RUN /opt/ue-demo/install_deps.sh

COPY . /opt/ue-demo/

# Keep working directory from base UE image
WORKDIR /opt/oai-nr-ue

EXPOSE 8080
