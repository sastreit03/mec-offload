"""Aetheredge ZMQ adapter for combined OAI state and UL weight messages."""

import zmq
from .legacy_wire import metrics_pb2, control_weights_pb2


class OAIAdapter:
    def __init__(self):
        self.context = zmq.Context()
        self.sub = self.context.socket(zmq.SUB)
        self.sub.setsockopt_string(zmq.SUBSCRIBE, "")
        self.sub.setsockopt(zmq.CONFLATE, 1)
        self.sub.connect("tcp://127.0.0.1:5555")
        self.pub = self.context.socket(zmq.PUB)
        self.pub.bind("tcp://127.0.0.1:5556")
        # Aetheredge: Runtime reads these fences without changing the legacy tuple API.
        self.source_generation = None
        self.report_sequence = None

    # Conflation supplies the newest sample, not a complete sequence of slot counters.
    def receive(self):
        try:
            raw = self.sub.recv(zmq.NOBLOCK)
        except zmq.Again:
            return None
        msg = metrics_pb2.Metrics()
        msg.ParseFromString(raw)
        # Aetheredge: Preserve source identity around cumulative grant counters.
        self.source_generation = msg.source_generation
        self.report_sequence = msg.report_sequence
        return msg.tti_cnt, {
            u.rnti: {
                f: getattr(u, f)
                for f in (
                    "rx_bytes",
                    "tx_bytes",
                    "ul_buffer",
                    "dl_buffer",
                    "snr",
                    "cqi",
                    "dl_tbs",
                    # Aetheredge: Derived capacity and temporary validation context.
                    "capacity_source_frame",
                    "capacity_source_slot",
                    "capacity_target_frame",
                    "capacity_target_slot",
                    "capacity_valid",
                    "capacity_bytes_per_prb",
                    "reference_prbs",
                    "reference_tbs_bits",
                    "ul_mcs",
                    "ul_mcs_table",
                    "ul_qm",
                    "ul_code_rate_x10240",
                    "ul_layers",
                    "ul_start_symbol",
                    "ul_symbols",
                    "ul_dmrs_re_per_prb",
                    "ul_rb_overhead",
                    "ul_tb_scaling",
                    "ul_transform_precoding",
                    # Aetheredge: Only new-data deltas will debit estimated demand.
                    "new_data_tbs_bytes_total",
                    "new_data_prbs_total",
                    "retransmission_tbs_bytes_total",
                    "retransmission_prbs_total",
                    "latest_grant_valid",
                    "latest_grant_retransmission",
                    "latest_grant_prbs",
                    "latest_grant_tbs_bytes",
                    "latest_grant_mcs",
                    "latest_grant_harq_round",
                    "latest_grant_target_frame",
                    "latest_grant_target_slot",
                    # Aetheredge: Explicit UL/DL link state and cumulative DL grants.
                    "radio_state_valid",
                    "radio_state_frame",
                    "radio_state_slot",
                    "scheduler_ul_mcs",
                    "scheduler_dl_mcs",
                    "ul_bler",
                    "dl_bler",
                    "pusch_snr_db",
                    "pucch_snr_db",
                    "link_quality_valid",
                    "dl_new_data_tbs_bytes_total",
                    "dl_new_data_prbs_total",
                    "dl_retransmission_tbs_bytes_total",
                    "dl_retransmission_prbs_total",
                    "latest_dl_grant_valid",
                    "latest_dl_grant_retransmission",
                    "latest_dl_grant_prbs",
                    "latest_dl_grant_tbs_bytes",
                    "latest_dl_grant_mcs",
                    "latest_dl_grant_harq_round",
                    "latest_dl_grant_frame",
                    "latest_dl_grant_slot",
                    # Aetheredge: Layer/direction remain explicit; this is not BSR.
                    "dl_rlc_buffer_bytes",
                    "dl_rlc_interface_sdu_bytes_total",
                    "ul_rlc_interface_sdu_bytes_total",
                    "rlc_counters_valid",
                )
            }
            for u in msg.ue_metrics
        }

    def send(self, index, ul):
        msg = control_weights_pb2.SchedulingWeights(ran_index=index)
        # No new DL controller is introduced. Empty DL map selects ordinary configured PF.
        for rnti, w in sorted(ul.items()):
            msg.ul_weights.extend([rnti, w])
        self.pub.send(msg.SerializeToString(), zmq.NOBLOCK)

    def close(self):
        self.sub.close(0)
        self.pub.close(0)
        self.context.term()
