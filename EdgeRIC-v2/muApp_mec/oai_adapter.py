"""One publisher; unchanged combined DL/UL wire format and echoed ran_index."""

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

    def receive(self):
        try:
            raw = self.sub.recv(zmq.NOBLOCK)
        except zmq.Again:
            return None
        msg = metrics_pb2.Metrics()
        msg.ParseFromString(raw)
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
