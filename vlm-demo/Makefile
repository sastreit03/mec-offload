# Makefile for installing MEC server and UE clients

# Default for typing "make"
.DEFAULT_GOAL := help

.PHONY: help prepare-mec prepare-ue

help:
	@echo "Specify a target:"
	@echo "  make prepare-mec"
	@echo "  make prepare-ue"

# Prepare MEC server
prepare-mec:
	./scripts/verify_srk.sh
	./scripts/verify_gpu.sh
	./scripts/build_mec_image.sh
	./scripts/download_model.sh
	./scripts/install_upf_patch.sh
	./scripts/compose_mec_image.sh
	@echo "Install is verified. SRK and MEC server are ready to start."
	@echo "Start SRK with ./../gnb/scripts/start_system.sh b200"
	@echo "Then start the MEC server with ./scripts/start_mec.sh"
	@echo "After starting MEC, verify MEC container is ready:"
	@echo "./scripts/verify_mec.sh"
	@echo "Finally, open the MEC dashboard on a browser with http://MEC_HOST_IP:8765/"

# Prepare UE client
prepare-ue:
	./scripts/verify_srk_ue.sh
	./scripts/build_ue_image.sh
	./scripts/install_ue_patch.sh
	@echo "Install is verified. SRK UE is ready to start."
	@echo "Start the UE with ./../ue/scripts/start_system_ue.sh b200"
	@echo "After starting the UE, check the connection between the UE and"
	@echo "the MEC server with ./scripts/check_ue_connection.sh"
	@echo "To start the MEC client application, run ./scritps/start_client.sh"
	@echo "Then, open the UE dashboard on a browser with http://HOST_IP:8080"
