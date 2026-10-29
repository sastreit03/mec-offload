# Staged acceptance record

The approved fixed-policy dataset campaign is specified separately in
`data-collection-mode.md`. That campaign fixes OAI to PF, keeps EdgeRIC in
shadow mode, fixes MEC candidates and CUDA priority, and drains all accepted
frames. It does not constitute acceptance of heuristic MEC or RAN scheduling.
The collection mode is implemented in source as of October 28, 2026. Live
single-UE and worst-case four-UE drain and memory validation remain pending.

Authoritative specifications: MEC architecture2.1 (2026-10-15, M01–M79), decision architecture4.4 (2026-10-15, D01–D82), OAI/EdgeRIC architecture1.5 (2026-10-19, R01–R67). Original DOCX bytes are bundled. Current user instructions override initial strict slot-deadline implementation and prohibit OAI edits.

| Stage | Acceptance and evidence | Current state |
|---|---|---|
| 0 Source/build/clock/identity | M05–M14, M26, M78–M79; R01–R04,R27–R29; hash supplied archives, preserve repaired scheduler, verify image digests/build inputs/net namespace/clocks | gNB, EdgeRIC, MEC and Linux UE were built and deployed across the two Sparks and Orin. Tunnel routing and MPS passed; image IDs, deployed config copies and model hashes were not captured. |
| 1 Shared contracts | M59–M67; R38–R46; validate RTP layout, fragmentation, duplicate/progress ordering, connection fencing, null missing metadata, replay | CPU contract tests and actual local gRPC/WebSocket mock integration pass. Java sources compile against Android35. Real encoder→GStreamer metadata/PTS survival and loss tests unrun. |
| 2 Demand replay | AC01 predicted→actual; AC02 repeated/reordered correction; AC03 overlapping/oldest vs earliest; AC04 expired-but-outstanding; AC05 reconnect/new context and old callbacks | Pure replay tests cover arithmetic/lifecycle; local integration checks early descriptor, old-connection fencing and result retention. Live radio-context reuse/service continuation remains unverified. |
| 3 Telemetry/capacity PF | D46–D50; R11–R23,R39–R44,R53;100-ms windows, counter layers/retries/overhead, fresh source, OAI reference TBS context, conversion | Live ZMQ reports, UE/RNTI association, capacity context and grant fields were observed. Reference-capacity accuracy, payload conversion and scheduler overhead remain outstanding. |
| 4 Shadow demand | R27,R38–R46; heuristic mutual awareness, proposed weights without allocation action, two→four UEs, background non-MEC, loss/reconnect | Shadow code sends no weight messages. Unit/integration behavior checked; actual OAI PF/shadow comparison and feedback-age/overhead profiling unrun. |
| 5 Heuristic weights | HC01 symmetry,HC02 capability,HC03 progress,HC04 urgency,HC05 reconciliation,HC06 fallback,HC07 coexistence,HC08 undersized caps,HC09 capacity/grants | HC01–HC05 arithmetic and omission behavior checked offline. OAI enforcement/live grants, non-MEC throughput, undersized-cap skips and resource budgets require testbed. Strict timing portions remain deferred. |
| 6 Complete heuristic pipeline | M16–M39,M64–M76; all12 warm model copies/nine streams; task quality; forced IDR/late plan handling; camera and dataset sources; concurrent tracking order; every transmitted frame denominator | One live detection video returned all 1,217 results; 1,167 completed inference and 602 met the 150-ms SLO. A 100-ms jitter window and 16-frame pending limit are current. Measured profiles, other applications, sustained multi-UE load and resource budgets remain outstanding. |
| 7 Future RL | M43,M67; D69–D82; R59–R67,MC01–MC06 | Policy Protocols/common validation boundary only. Remote application-agent implementation, RL choices/training and MC acceptance deferred; not counted as heuristic completion. |

Use `requirements-to-validation.csv` for component-level traceability, `test-results.txt` for the actual final CPU checks, and `android-javac.txt` for the API compile result. One warning from FastAPI's test client concerns a future httpx→httpx2 transition; the checks pass with the recorded environment.

No source inspection, mock result, successful Java compilation or future container build is labeled live-system validation. “Implemented” means source exists, not that acceptance has passed. Several live gates are deliberately unsatisfied; synthetic settings cannot pass experimental startup.

Initial run sequence: one UE per application; then two/four simultaneous MEC UEs; add non-MEC traffic that never contacts MEC; inject loss/reordering; change IP/reconnect/restart gNB and EdgeRIC; force RNTI disappearance/reuse; test ambiguity; compare PF and shadow; only then enable weights. Preserve offered load, probe intervals, admitted/suspended attempts, old applied plans, missing results and late amendments in analysis. Report measured distributions, not just a successful build or average latency.
