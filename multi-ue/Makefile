SHELL := /bin/bash
.PHONY: bootstrap test mec edge build-mec build-edge
bootstrap:
	bash scripts/bootstrap.sh
test:
	source scripts/environment.sh && python -m pytest tests -q
mec:
	source scripts/environment.sh && python -m mec_server.app --config config/mec.experimental.yaml
edge:
	source scripts/environment.sh && python -m muApp_mec --config config/edgeric.shadow.yaml
build-mec:
	docker build -f mec/docker/Dockerfile.mec -t multi-ue-mec:baseline .
build-edge:
	docker build -f mec/docker/Dockerfile.edgeric -t multi-ue-edgeric:baseline ..
