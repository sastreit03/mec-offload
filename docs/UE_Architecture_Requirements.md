---
title: "UE architecture requirements"
subtitle: "Design decision record | Version 1.0 | October 26 2026"
author: "Aetheredge"
subject: "Linux and Android UE media-source and prerecorded-video lifecycle requirements"
---

This record specifies the agreed UE behavior for the Aetheredge streaming MEC
testbed. It complements MEC server architecture v2.1, MEC Decision Maker
Architecture v4.4, and OAI/EdgeRIC architecture v1.5. U numbers identify the
requirements in this record. The continuous cross-video behavior applies only
to prerecorded video directories; camera streams retain their existing
continuous-session behavior.

# 1. Platform and network identity

**U01  Supported UE platforms.** Support a Linux UE, including an NVIDIA Jetson
AGX Orin, and an Android UE. Both clients shall use the same MEC control and
media contracts where their platform APIs permit.

**U02  Dynamic UE address.** The UE shall discover and use the IP address
assigned by the mobile core. A deployment shall not depend on a hard-coded
address such as `12.1.1.2`.

**U03  Linux interface selection.** The Linux client shall select the UE tunnel
by interface name and bind media/control behavior to the address currently
assigned to that interface. The expected default deployment interface is
configurable, such as `oaitun_ue1`.

**U04  Android network binding.** The Android client shall bind its streaming
session to the selected cellular network and update that binding if the
network-assigned address changes.

# 2. Media sources and local storage

**U05  Camera source.** Both UE implementations may stream from a live camera.
Camera streaming shall keep its existing single continuous-video lifecycle and
shall not invoke prerecorded cross-video preparation or randomized selection.

**U06  Single-file source.** A UE may stream one explicitly selected
prerecorded file using the existing one-video lifecycle. Selecting one file
does not implicitly enable directory continuation.

**U07  Prerecorded directory.** A UE shall be able to store and select a local
directory containing BDD100K or comparable prerecorded videos. Linux accepts a
directory path. Android uses the system document-tree picker and retains read
permission when the platform grants it.

**U08  File discovery.** Directory playback shall recursively discover the
supported video files. The initial supported extensions are MP4, MOV, MKV, and
M4V, matched without case sensitivity. An empty directory or unreadable
selection shall fail clearly before streaming begins.

# 3. Randomized playlist behavior

**U09  Random order.** At the start of each playlist cycle, the UE shall shuffle
the discovered prerecorded videos rather than use a fixed filename or upload
order.

**U10  Complete cycle.** Every discovered video shall be selected exactly once
per playlist cycle before any video is selected for the next cycle.

**U11  Cycle boundary.** When more than one video exists, the first selection
of a new cycle shall not equal the final selection of the preceding cycle.

**U12  Reproducibility.** The UE shall support an optional shuffle seed for
repeatable experiments. Without an explicit seed it shall choose a seed for the
run. Logs shall record the seed, selected file, cycle number, and position.

# 4. Prerecorded-video handoff

**U13  Advance preparation.** Shortly before a prerecorded video reaches end
of input, the UE shall request preparation of the already selected successor.
The request shall identify the current video as the predecessor and provide an
expected successor start time. The preparation lead is configurable by the MEC
and defaults to five seconds.

**U14  Separate session identity.** The successor shall have a distinct video
identifier, epoch, receiver/port assignment, frame sequence, configuration
history, and per-video processing state. Frame numbering restarts for the new
video.

**U15  End notification.** The UE shall send `video_end` for every completed
prerecorded video so the MEC can close input, drain accepted work, preserve
evidence, and reset state such as the object tracker.

**U16  No end-of-video wait.** After queuing the predecessor's `video_end`, the
UE shall start the prepared successor locally without waiting for a MEC
completion response. The first successor frame may immediately follow the last
predecessor frame.

**U17  Arrival-order tolerance.** The MEC shall safely promote a prepared
successor whether it observes the predecessor's end control message or the
successor's first media descriptor first. A late terminal status for the
predecessor shall not stop or replace the active successor.

**U18  Late local startup.** If local preparation finishes after the planned
successor boundary, the UE shall begin from the current time. It shall not emit
a catch-up burst for release times already in the past.

**U19  Preparation failure.** If advance preparation is rejected, times out,
or fails, the UE shall retain the selected successor and request ordinary
admission after the predecessor terminates. This fallback may introduce an
inter-video delay but shall not silently skip the selection.

**U20  Explicit stop.** A user-requested end shall disable playlist
continuation and end the active or prepared sessions cleanly.

# 5. Media and control consistency

**U21  Frame types.** A transmitted descriptor shall use type `1` for an IDR
keyframe or type `3` for a P frame. The UE shall not transmit B frames or
non-IDR I-frame descriptor types.

**U22  Configuration scope.** Application, SLO, input shape, and active
bitrate/model configuration shall remain consistent across advance preparation
unless a later architecture decision explicitly permits a per-video change.
The successor receives fresh plan and configuration versions even when the
selected values are inherited.

**U23  Suspension scope.** A prepared successor shall not inherit stale stop or
suspension fields from its predecessor. Once active, it follows the ordinary
versioned MEC transmission-control protocol.

**U24  Evidence.** UE and MEC logs shall retain playlist selection, preparation
request and response, promotion trigger, fallback reason, video end, and final
status with enough video and UE identity to reconstruct the handoff.

# 6. Acceptance checks

**UA01  Address discovery.** Start each UE with a core-assigned address that is
not `12.1.1.2`; verify that control and RTP traffic originate from the assigned
UE interface/address.

**UA02  Playlist ordering.** With at least three files, verify that each file is
selected once per cycle, two runs with the same seed have the same order, and
the cycle boundary does not immediately repeat a file.

**UA03  Camera isolation.** Start a camera source and verify that it sends no
successor-preparation request and retains the existing camera lifecycle.

**UA04  Control-first handoff.** Deliver `video_end` before the successor's
first media packet; verify immediate promotion, a fresh tracker, and continued
successor processing.

**UA05  Media-first handoff.** Deliver the successor's first descriptor before
`video_end`; verify the same final state and verify that the later predecessor
status cannot stop the successor.

**UA06  Preparation failure.** Reject or time out advance preparation and
verify ordinary admission of the same selected file after the old session
terminates.

**UA07  Sustained playback.** Run multiple complete playlist cycles and verify
that session/receiver capacity is reclaimed, evidence remains attributable to
the correct video, and memory and file descriptors remain bounded.

**UA08  Platform validation.** Run the checks on both the Linux UE and Android
UE using their deployment toolchains. Android shall be compiled with JDK 17
and the configured Android SDK before installation.

# 7. Current implementation status

U01–U24 are implemented in the current working tree. Offline tests cover
seeded playlist cycling and the two control/media arrival orders. Live Linux
and Android execution, core-assigned address changes, sustained multi-cycle
playback, and end-to-end MEC/RAN behavior remain acceptance work rather than
completed validation.

