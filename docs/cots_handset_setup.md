# How to Setup COTS Handset in OpenAirInterface 5G

This document provides guidance on how to set up a COTS UE running on OpenAirInterface 5G's SA network stack. OAI `2025.w34` branch with Sionna RK is the specific implementation used.

## Handsets Used

Of those tested, the only handset that has successfully attached to the network is the **Google Pixel 7**. The Android versions that have been verified on Pixel 7s as working are **Android 14** and **Android 16**. No other Android versions have been tested in the lab on a Google Pixel 7. Google Pixel 5 and Pixel 6 did not connect to OAI.

## References and Resources

The following references and resources were followed in order to connect the handsets to the network and to create this guide.
- [Programming SIM Cards](https://nvlabs.github.io/sionna/rk/setup/sim.html): This Sionna RK tutorial shows how to program a sysmocom SIM card.
- [Sysmocom SIM Card Info](https://docs.google.com/spreadsheets/d/1Un61NVeW-8GATpOxu8_gZKnmUXvy_r9plhUD0lrHdEU/edit?gid=0#gid=0): This Google Sheet contains info about the sysmocom programmable SIM cards that Ryan ordered. It is likely necessary to ask Ryan for access to this sheet since he is the owner. The sysmocom SIM cards are on the metal shelves in the lab, straight ahead from the door. The info in this sheet is necessary to program the card, as explained below.
- [OAI COTS Handset Documentation](https://github.com/openairinterface/oai-cn5g-fed/blob/develop/docs/LIST_OF_TESTED_COTSUE.md): This page documents the COTS handsets that OAI has successfully connected to its network. The bottom of the page has settings to change on a Pixel 7.

## 1. Change the MCC and MNC of the gNB and core

By default in Sionna RK and its implementation of OAI, the MCC and MNC of the gNB are 262 and 99, respectively. These settings did not work with our handsets. The MCC and MNC need to be changed to 001 and 01. These are research/test values.

- **Change MCC and MNC in the gNB config file.** In `$SIONNA_RK_ROOT/config/common/gnb.sa.band78.*prbs.conf`, near the top of the file is `plmn_list`. In that list, change `mcc` from `262` to `001` and `mnc` from `99` to `01`.
- **Change the MCC and MNC in the core network.** In `$SIONNA_RK_ROOT/config/common/sys_config.yaml` are the core network settings. In the file, look for `amf`, and in that `served_guami_list` and `plmn_support_list`. There are `mcc` and `mnc` in each. Change them from `262` to `001` and from `99` to `01`, respectively.
- **Add UE's in the core network subscriber table.** The file `$SIONNA_RK_ROOT/config/common/oai_db.sql` contains the core network's subscriber info. Info on a UE's SIM card must match the information in this file. 
  - Toward the bottom of the file are `INSERT INTO` commands. Copy one of these `INSERT` lines and paste it below the other `INSERT` commands. Choose a line whose `key` field is `0xfec86ba6eb707ed08905757b1bb44b8f` and whose `OPc` field is `0xc42449363bbad02b66d16bc975d77cc1`. These were chosen because they match the Sionna RK tutorial for programming the SIM cards. These `key` and `OPc` fields will be programmed into the SIM card.
  - Change the `IMEI` (the third field on the INSERT, but confirm the order) to the handset's IMEI. The IMEI is unique to each individual handset. To find the handset's IMEI, do these steps on the handset:
    - Open the Phone app and type `*#*#4636#*#*`.
    - Select "Phone Information" and look for `IMEI`.
  - Change the `IMSI` in the `INSERT` command. The `IMSI` is the first field of the `INSERT`. The original `IMSI` that was copied can be used, but the first five digits of the `IMSI` need to be changed. They should be set to `00101`, because the first five digits of the `IMSI` are the MCC and MNC.

## 2. Program the SIM Card

A few prerequisites need to be done before programming the SIM card:
- pySim is used to program the sysmocom SIM cards. Create a python virtual environment and into it install pySim's dependencies.
- Get the ADM password for the SIM card. The existing IMSI on the SIM card is printed on the blue sysmocom card in which the SIM card came. Use this IMSI to locate the row of the [SIM card Google sheet](https://docs.google.com/spreadsheets/d/1Un61NVeW-8GATpOxu8_gZKnmUXvy_r9plhUD0lrHdEU/edit?gid=0#gid=0). Find `ADM1` field of the table. This password is used in the [Sionna RK tutorial](https://nvlabs.github.io/sionna/rk/setup/sim.html). Without it, the SIM cannot be programmed. **Note:** The ADM should be eight digits. If it is less than eight digits in the table, add leading 0s at the beginning of the ADM until there are eight digits when entering the ADM in pySim.

With pySim and the ADM, the follow the Sionna RK tutorial for programming the SIM card.
- First, verify the ADM.
- Set the `IMSI` to the value in the core network subscriber table. Sionna RK says that the MNC is `262` and the MCC `99`, but we changed those to `001` and `01`, so the first five digits of the `IMSI` will be `00101`.
- Set the `key` and `op_opc` according to the `key` and `OPc` fields in the subscriber table.
- Set the PLMN to match `001` and `01`, not `262`, `99`.
- Set the MSISDN. This is like the phone number, so if you add another handset, increment the number.
- Follow the tutorial for setting `ACC1` and for setting the `SPN` to `OpenAirInterface`.

The SIM card is now programmed and can be inserted into the handset.

## 3. Change settings on the handset.

Don't change the following settings on the handset until after the SIM card is put in the handset. Otherwise, they will not save.

- **Set the information in the phone menu**. Open the phone app and change the following info according to [OAI's documentation](https://github.com/openairinterface/oai-cn5g-fed/blob/develop/docs/LIST_OF_TESTED_COTSUE.md).
  - Type `*#*#4636#*#*` in the keypad. Click on "Phone Information" and change `Set Preferred Network Type:` to `NR only`.
  - Return to the phone app and type `*#*#0702#*#*`. Look for two settings:
    - `NR_TIMER_WAIT_IMS_REGISTRATION`: change from `180` to `-1`.
    - `SUPPORT_IMS_NR_REGISTRATION_TIMER`: change from `1` to `0`.
- **Set network info in settings**: Open the Settings app and click Network & internet.
  - Disable WiFi.
  - Set the APN:
    - Click on `SIMs`. It should show "OpenAirInterface." Then click on `OpenAirInterace`, and then scroll down and click `Access Point Names`. Disable and/or delete any existing APNs.
    - Create a new APN with the following settings:
      - `Name`: `oai`
      - `APN`: `oai`
      - `MCC` (if available to be changed): `001`
      - `MNC` (if available to be changed): `01`
      - Leave other settings unchanged.

## 4. Start OAI and allow the phone to connect

Run OAI and ensure that the handset is not on airplane mode. It may take a minute or two before the handset attaches. Watch these the gNB and AMF logs and the connection status on the handset:
- `docker logs -f oai-gnb` will show UE statistics when a UE attaches.
- `docker logs -f oai-amf` will show UE settings when a UE attaches to a gNB that is attached to the AMF.
- The top menu bar on the handset will change to show that it has attached to the gNB.