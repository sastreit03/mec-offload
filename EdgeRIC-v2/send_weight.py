from edgeric_messenger import EdgericMessenger
import threading
import time
import numpy as np

class SendWeight:
    def __init__(self):
        self.messenger = EdgericMessenger(socket_type="weights")

    def periodic_send_weight(self):
        while True:
            tti_count, ue_dict = self.messenger.get_metrics(True)                # get metrics
            # if tti_count is not None:
            dl_weights, ul_weights = self.generate_weight_array(ue_dict)                   # compute policy
            self.messenger.send_scheduling_weight(tti_count, 
                dl_weights, 
                True,
                ul_weights=ul_weights) # send policy
    
    def generate_weight_array(self, ue_dict):
        dl_weights = []
        ul_weights = []
        #weight_array = [
        #31282, 0.7,  # RNTI 1001 with weight 0.5
        #60481, 0.3,  # RNTI 1002 with weight 0.3  # RNTI 1003 with weight 0.2
        # ]   
        iii=0.05
        iii=0.5
        for rnti in ue_dict.keys():
            #weight_value = iii
            dl_weights.extend([rnti, 1.0])
            ul_weights.extend([rnti, 1.0])
            #weight_array.extend([rnti, weight_value])
            #iii=iii
        return dl_weights, ul_weights


if __name__ == "__main__":
    send_weight = SendWeight()

    # Create and start the periodic sending thread
    send_weight_thread = threading.Thread(target=send_weight.periodic_send_weight)
    send_weight_thread.start()

    # Keep the main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping the weight sending script.")
