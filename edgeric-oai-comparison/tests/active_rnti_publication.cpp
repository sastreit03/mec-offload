// Run in an isolated network namespace: this exercises the real ZMQ publisher.
#include "edgeric.h"
#include <cassert>
#include <chrono>
#include <thread>
#include <iostream>

int main() {
  edgeric::init();
  zmq::context_t ctx;
  zmq::socket_t sub(ctx, ZMQ_SUB);
  sub.set(zmq::sockopt::subscribe, "");
  sub.set(zmq::sockopt::rcvtimeo, 2000);
  sub.connect("tcp://127.0.0.1:5555");
  std::this_thread::sleep_for(std::chrono::milliseconds(300));
  auto report = [&](const uint16_t *active, uint32_t count) {
    edgeric::send_to_er(active, count);
    zmq::message_t raw;
    assert(sub.recv(raw));
    Metrics msg;
    assert(msg.ParseFromArray(raw.data(), raw.size()));
    assert(msg.ue_metrics_size() == static_cast<int>(count));
    for (uint32_t i = 0; i < count; ++i) assert(msg.ue_metrics(i).rnti() == active[i]);
    return msg;
  };
  // Historical entries exist in all maps that formerly determined membership.
  edgeric::set_cqi(99, 12);
  edgeric::set_ul_capacity(99, AetheredgeUlCapacity{});
  edgeric::set_radio_state(99, AetheredgeRadioState{});
  edgeric::record_dl_grant(99, false, 4, 400, 1, 0, 1, 1);
  edgeric::record_ul_grant(11, false, 3, 300, 1, 0, 1, 1);
  const uint16_t both[] = {11, 22}; // 22 is active but has no telemetry yet.
  auto first = report(both, 2);
  assert(first.ue_metrics(0).new_data_tbs_bytes_total() == 300);
  const uint16_t remaining[] = {22};
  report(remaining, 1); // 11 and historical 99 must not leak into publication.
  report(nullptr, 0);
  auto restored = report(both, 2);
  assert(restored.ue_metrics(0).new_data_tbs_bytes_total() == 300);
  assert(restored.report_sequence() == first.report_sequence() + 3);
  std::cout << "Active-only publication and retained counters passed\n";
}
