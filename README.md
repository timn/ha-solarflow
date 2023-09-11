SolarFlow Local MQTT Bridge and Controller
==========================================

This class reads input from SolarFlow via (redirected) MQTT from the device and
converts it to topics and discovery information compatible to HomeAssistant. It
also reacts to time requests and thus allows to completely disconnect the device
from the vendor cloud and using and controlling it purely locally.

Overview
--------

Mosquitto
AdGuard
Dnsmasq
AppDaemon
