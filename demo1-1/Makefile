# Makefile for installing MEC server and UE clients

# Default for typing "make"
.DEFAULT_GOAL := help

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
	./scripts/verify_srk_ue.sh
	./scripts/build_ue_image.sh
	./scripts/install_ue_patch.sh
	@echo "Install is verified. SRK UE is ready to start."
	@echo "Start the UE with ./../ue/scripts/start_system_ue.sh b200"
	@echo "After starting the UE, check the connection between the UE and"
	@echo "the MEC server with ./scripts/check_ue_connection.sh"
	@echo "Then, test inference on the MEC server with"
	@echo "./scripts/run_ue_client.sh"
