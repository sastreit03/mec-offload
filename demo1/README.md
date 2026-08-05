# Demo 1
Contains code for first implementation of MEC server.

## Layout

- `documentation/`: results of implementation steps.
- `mec-server/`: code that builds the MEC server and runs initial basic inference test.
- `ue-client/`: code that modifies the oai-nr-ue container and sends an image to the MEC server for inference.
- `scripts/`: shell scripts to be run on the MEC server side and on the UE side. Functionality includes starting and stopping MEC, testing MEC readiness, and sending inference task from UE.
