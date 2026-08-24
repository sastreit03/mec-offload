# Makefile for installing MEC server and UE clients

# Default for typing "make"
.DEFAULT_GOAL := help

.PHONY: help prepare-mec prepare-ue prepare-mec-server-sa

help:
	@echo "Specify a target:"
	@echo "  make prepare-mec"
	@echo "  make prepare-ue"
	@echo "  make prepare-mec-server-sa"

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
	@echo "After starting SRK and MEC, verify MEC server is ready by running:"
	@echo "./scripts/verify_mec_active.sh"

# Prepare UE client
prepare-ue:
	./scripts/verify_srk_ue.sh
	./scripts/build_ue_image.sh
	./scripts/install_ue_patch.sh
	@echo "Install is verified. SRK UE is ready to start."
	@echo "Start the UE with ./../ue/scripts/start_system_ue.sh b200"
	@echo "After starting the UE, check the connection between the UE and"
	@echo "the MEC server with ./scripts/check_ue_connection.sh"
	@echo "Then, open the UE dashboard on a browser with http://HOST_IP:8080"

# Preapre MEC server for standalone installation apart from OAI
prepare-mec-server-sa:
	./scripts/verify_gpu.sh
	./scripts/build_mec_image_sa.sh
	./scripts/download_model.sh
	./scripts/compose_mec_image_sa.sh
	@echo "Install is verified. MEC server is ready to start."
	@echo "Start the MEC server with ./scripts/start_mec_sa.sh"
	@echo "After starting MEC, verify MEC server is ready by running:"
	@echo "./scripts/verify_mec_active_sa.sh"
