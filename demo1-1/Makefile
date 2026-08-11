# Makefile for installing MEC server and UE clients

# Default for typing "make"
.DEFAULT_GOAL := help

#SCRIPTS_DIR := demo1-1/scripts

.PHONY: help prepare-mec-server prepare-ue-client

help:
	@echo "Specify a target:"
	@echo "  make prepare-mec-server"
	@echo "  make prepare-ue-client"

# Prepare MEC server
prepare-mec-server:
	./scripts/verify_srk.sh
	./scripts/verify_gpu.sh
	./scripts/build_mec_image.sh
	./scripts/download_model.sh
	./scripts/install_upf_patch.sh
	./scripts/compose_mec_image.sh
	@echo "Install is verified. SRK and MEC server are ready to start."
	@echo "Start SRK with ./../gnb/scripts/start_system.sh b200"
	@echo "Then start the MEC server with ./scripts/start_mec.sh"
	@echo "After starting SRK and MEC, verify MEC server is ready by running:"
	@echo "./scripts/verify_mec_active.sh"

# Prepare UE client
prepare-ue-client:
	@:
