# Copyright (C) 2023 Tim Niemueller

from typing import Any, Callable, Optional, Union

import hashlib
import json
import mqttapi as mqtt
import random
import sys
import time

SEND_DISCOVERY_INTERVAL_SEC = 10
REQUEST_ALL_INTERVAL_SEC = 1800
DISCOVERY_PREFIX = 'homeassistant'

PV_BRANDS = {
  'other': 0,
  'hoymiles': 1,
  'enphase' : 2,
  'apsystems': 3,
  'anker': 4,
  'deye': 5,
  'bosswerk': 15,
}

BYPASS_MODES = {
  'automatic': 0,
  'always_off': 1,
  'always_on' : 2,
}

class SolarFlow(mqtt.Mqtt):
  """SolarFlow MQTT bridge.

  This class reads input from SolarFlow via (redirected) MQTT from the device
  and converts it to topics and discovery information compatible to
  HomeAssistant. It also reacts to time requests and thus allows to completely
  disconnect the device from the vendor cloud and using and controlling it
  purely locally.
  """

  topic_prefix: str
  topic_callbacks: dict[str, Callable]
  command_topics: list[str]
  device_id: str
  device_serial: Optional[str]
  module_versions: dict[str, int]
  battery_packs: list[str]
  topics: dict[str, Any]
  cache: dict[str, Union[str, int]]

  def initialize(self) -> None:
    self.set_namespace('mqtt')
    self.topic_prefix = self.args['topic_prefix']
    self.device_id = self.args['device_id']
    self.device_serial = None
    self.module_versions = {}
    self.topics = {}
    self.battery_packs = []
    self.command_topics = []
    self.cache = {}

    self.listen_event(self.message_received, 'MQTT_MESSAGE')

    self.topic_callbacks = {
      self.topic_name_for('firmware/report'): self.firmware_report_received,
      self.topic_name_for('time-sync'): self.time_sync_received,
      self.topic_name_for('properties/report'): self.properties_report_received,
      self.topic_name_for('log'): self.log_received,
    }
    self.log(f'Subscribing to topics [{", ".join(self.topic_callbacks.keys())}]')

    for topic in self.topic_callbacks.keys():
      self.mqtt_subscribe(topic)

    self.try_send_discovery()

  def terminate(self) -> None:
    for topic in self.topic_callbacks.keys():
      self.mqtt_unsubscribe(topic)

    for topic in self.command_topics:
      self.mqtt_unsubscribe(topic)


  def topic_name_for(self, suffix: str, *, command: bool = False) -> str:
    if command:
      return f'iot/{self.topic_prefix}/{self.device_id}/{suffix}'
    else:
      return f'/{self.topic_prefix}/{self.device_id}/{suffix}'

  def get_device_info(self):
    return {
      'name': 'SolarFlow',
      'manufacturer': 'Zendure',
      'model': 'SolarFlow',
      'identifiers': [self.device_id, self.device_serial],
    }

  def generate_timestamp(self) -> int:
    return int(time.time() * 1000)

  def generate_message_id(self) -> int:
    return hashlib.md5(str(random.randint(0,sys.maxsize)).encode(),
                       usedforsecurity=False).hexdigest()

  def create_request(self, args: dict[str, Any]) -> str:
    header = {
      'timestamp': self.generate_timestamp(),
      'messageId': self.generate_message_id(),
      'deviceId': self.device_id,
    }
    return json.dumps(header | args)

  def request_all(self):
    args = {
      'properties': ['getAll']
    }
    self.mqtt_publish(self.topic_name_for('properties/read', command=True),
                      self.create_request(args))

  def periodic_request_all(self, cb_args = {}):
    self.request_all()
    self.run_in(self.periodic_request_all, REQUEST_ALL_INTERVAL_SEC)

  def state_topic(self, item: str) -> str | None:
    if item not in self.topics:
      return None
    return self.topics[item]['config']['state_topic']

  def try_send_discovery(self, cb_args: dict[str, Any] = {}) -> None:
    if self.device_serial is None:
      self.log('Device serial not yet known, not sending discovery info')
      self.request_all()
      self.run_in(self.try_send_discovery, SEND_DISCOVERY_INTERVAL_SEC)
      return

    if not self.battery_packs:
      self.log('Battery packs not yet known (or no batteries installed)')
      self.request_all()

    if 'max_inverter_input' not in self.cache:
      self.log('Max inverter input not yet known, not sending discovery info')
      self.request_all()
      self.run_in(self.try_send_discovery, SEND_DISCOVERY_INTERVAL_SEC)
      return

    self.log('Sending discovery info')
    node_id = f'solarflow_{self.device_serial}_{self.device_id}'
    device_info = self.get_device_info()

    new_topics = {
      'battery': {
        'config_topic': f'{DISCOVERY_PREFIX}/sensor/{node_id}/battery_soc/config',
        'config': {
          'device': device_info,
          'device_class': 'battery',
          'unit_of_measurement': '%',
          'name': 'SolarFlow Battery',
          'object_id': 'solarflow_battery',
          'state_topic': 'solarflow/battery/state',
          'unique_id': f'{node_id}_battery',
        }
      },
      'state': {
        'config_topic': f'{DISCOVERY_PREFIX}/sensor/{node_id}/state/config',
        'config': {
          'device': device_info,
          'name': 'SolarFlow State',
          'object_id': 'solarflow_state',
          'state_topic': 'solarflow/state',
          'unique_id': f'{node_id}_state',
        }
      },
      'bypass_mode': {
        'command_callback': self.set_bypass_mode,
        'config_topic': f'{DISCOVERY_PREFIX}/select/{node_id}/bypass_mode/config',
        'config': {
          'device': device_info,
          'name': 'SolarFlow Bypass Mode',
          'object_id': 'solarflow_bypass_mode',
          'state_topic': 'solarflow/bypass_mode/state',
          'command_topic': 'solarflow/bypass_mode/set',
          'unique_id': f'{node_id}_bypass_mode',
          'icon': 'mdi:domain',
          'options': list(BYPASS_MODES.keys()),
        }
      },
      'home_output_power': {
        'config_topic': f'{DISCOVERY_PREFIX}/sensor/{node_id}/home_output_power/config',
        'start_value': 0,
        'config': {
          'device': device_info,
          'device_class': 'power',
          'unit_of_measurement': 'W',
          'name': 'SolarFlow Home Output Power',
          'object_id': 'solarflow_home_output_power',
          'state_topic': 'solarflow/home_output_power/state',
          'unique_id': f'{node_id}_home_output_power',
          'icon': 'mdi:home-import-outline',
        }
      },
      'battery_output_power': {
        'config_topic': f'{DISCOVERY_PREFIX}/sensor/{node_id}/battery_output_power/config',
        'start_value': 0,
        'config': {
          'device': device_info,
          'device_class': 'power',
          'unit_of_measurement': 'W',
          'name': 'SolarFlow Battery Output Power',
          'object_id': 'solarflow_battery_output_power',
          'state_topic': 'solarflow/battery_output_power/state',
          'unique_id': f'{node_id}_battery_output_power',
          'icon': 'mdi:battery-arrow-up',
        }
      },
      'solar_input_power': {
        'config_topic': f'{DISCOVERY_PREFIX}/sensor/{node_id}/solar_input_power/config',
        'start_value': 0,
        'config': {
          'device': device_info,
          'device_class': 'power',
          'unit_of_measurement': 'W',
          'name': 'SolarFlow Solar Input Power',
          'object_id': 'solarflow_solar_input_power',
          'state_topic': 'solarflow/solar_input_power/state',
          'unique_id': f'{node_id}_solar_input_power',
          'icon': 'mdi:solar-power-variant-outline',
        }
      },
      'solar_input_1_power': {
        'config_topic': f'{DISCOVERY_PREFIX}/sensor/{node_id}/solar_input_1/config',
        'start_value': 0,
        'config': {
          'device': device_info,
          'device_class': 'power',
          'unit_of_measurement': 'W',
          'name': 'SolarFlow Solar Input 1 Power',
          'object_id': 'solarflow_solar_input_1_power',
          'state_topic': 'solarflow/solar_input_1_power/state',
          'unique_id': f'{node_id}_solar_input_1_power',
          'icon': 'mdi:solar-power-variant-outline',
        }
      },
      'solar_input_2_power': {
        'config_topic': f'{DISCOVERY_PREFIX}/sensor/{node_id}/solar_input_2/config',
        'start_value': 0,
        'config': {
          'device': device_info,
          'device_class': 'power',
          'unit_of_measurement': 'W',
          'name': 'SolarFlow Solar Input 2 Power',
          'object_id': 'solarflow_solar_input_2_power',
          'state_topic': 'solarflow/solar_input_2_power/state',
          'unique_id': f'{node_id}_solar_input_2_power',
          'icon': 'mdi:solar-power-variant-outline',
        }
      },
      'solar_overflow_power': {
        'config_topic': f'{DISCOVERY_PREFIX}/sensor/{node_id}/solar_overflow_power/config',
        'start_value': 0,
        'config': {
          'device': device_info,
          'device_class': 'power',
          'unit_of_measurement': 'W',
          'name': 'SolarFlow Solar Overflow Power',
          'object_id': 'solarflow_solar_overflow_power',
          'state_topic': 'solarflow/solar_overflow_power/state',
          'unique_id': f'{node_id}_solar_overflow_power',
          'icon': 'mdi:solar-power-variant-outline',
        }
      },
      'battery_input_power': {
        'config_topic': f'{DISCOVERY_PREFIX}/sensor/{node_id}/battery_input_power/config',
        'start_value': 0,
        'config': {
          'device': device_info,
          'device_class': 'power',
          'unit_of_measurement': 'W',
          'name': 'SolarFlow Battery Input Power',
          'object_id': 'solarflow_battery_input_power',
          'state_topic': 'solarflow/battery_input_power/state',
          'unique_id': f'{node_id}_battery_input_power',
          'icon': 'mdi:battery-arrow-down',
        }
      },
      'battery_charge_time': {
        'config_topic': f'{DISCOVERY_PREFIX}/sensor/{node_id}/battery_charge_time/config',
        'start_value': 0,
        'config': {
          'device': device_info,
          'device_class': 'duration',
          'unit_of_measurement': 'min',
          'name': 'SolarFlow Battery Charge Time',
          'object_id': 'solarflow_battery_charge_time',
          'state_topic': 'solarflow/battery_charge_time/state',
          'unique_id': f'{node_id}_battery_charge_time',
          'icon': 'mdi:progress-clock',
        }
      },
      'battery_runtime': {
        'config_topic': f'{DISCOVERY_PREFIX}/sensor/{node_id}/battery_runtime/config',
        'config': {
          'device': device_info,
          'device_class': 'duration',
          'unit_of_measurement': 'min',
          'name': 'SolarFlow Battery Runtime',
          'object_id': 'solarflow_battery_runtime',
          'state_topic': 'solarflow/battery_runtime/state',
          'unique_id': f'{node_id}_battery_runtime',
          'icon': 'mdi:progress-clock',
        }
      },
      'min_soc': {
        'command_callback': self.set_min_soc,
        'config_topic': f'{DISCOVERY_PREFIX}/number/{node_id}/min_soc/config',
        'config': {
          'device': device_info,
          'unit_of_measurement': '%',
          'name': 'SolarFlow Min Capacity',
          'object_id': 'solarflow_min_soc',
          'state_topic': 'solarflow/min_soc/state',
          'command_topic': 'solarflow/min_soc/set',
          'unique_id': f'{node_id}_min_soc',
          'min': 0,
          'max': 30,
          'step': 1,
          'mode': 'slider',
          'icon': 'mdi:battery-10',
        }
      },
      'max_soc': {
        'command_callback': self.set_max_soc,
        'config_topic': f'{DISCOVERY_PREFIX}/number/{node_id}/max_soc/config',
        'config': {
          'device': device_info,
          'unit_of_measurement': '%',
          'name': 'SolarFlow Max Capacity',
          'object_id': 'solarflow_max_soc',
          'state_topic': 'solarflow/max_soc/state',
          'command_topic': 'solarflow/max_soc/set',
          'unique_id': f'{node_id}_max_soc',
          'min': 70,
          'max': 100,
          'step': 1,
          'mode': 'slider',
          'icon': 'mdi:battery-90',
        }
      },
      'max_inverter_input': {
        'command_callback': self.set_max_inverter_input,
        'config_topic': f'{DISCOVERY_PREFIX}/number/{node_id}/max_inverter_input/config',
        'config': {
          'device': device_info,
          'unit_of_measurement': 'W',
          'name': 'SolarFlow Max Inverter Input',
          'object_id': 'solarflow_max_inverter_input',
          'state_topic': 'solarflow/max_inverter_input/state',
          'command_topic': 'solarflow/max_inverter_input/set',
          'unique_id': f'{node_id}_max_inverter_input',
          'min': 0,
          'max': 1200,
          'step': 100,
          'mode': 'slider',
          'icon': 'mdi:meter-electric-outline',
        }
      },
      'output_limit': {
        'command_callback': self.set_output_limit,
        'config_topic': f'{DISCOVERY_PREFIX}/number/{node_id}/output_limit/config',
        'config': {
          'device': device_info,
          'unit_of_measurement': 'W',
          'name': 'SolarFlow Output Limit',
          'object_id': 'solarflow_output_limit',
          'state_topic': 'solarflow/output_limit/state',
          'command_topic': 'solarflow/output_limit/set',
          'unique_id': f'{node_id}_output_limit',
          'min': 0,
          'max': self.cache['max_inverter_input'],
          'step': 1,
          'mode': 'slider',
          'icon': 'mdi:export',
        }
      },
      'buzzer_switch': {
        'command_callback': self.set_buzzer_switch,
        'config_topic': f'{DISCOVERY_PREFIX}/switch/{node_id}/buzzer_switch/config',
        'config': {
          'device': device_info,
          'name': 'SolarFlow Buzzer Switch',
          'object_id': 'solarflow_buzzer_switch',
          'state_topic': 'solarflow/buzzer_switch/state',
          'command_topic': 'solarflow/buzzer_switch/set',
          'unique_id': f'{node_id}_buzzer_switch',
          'icon': 'mdi:surround-sound',
        }
      },
      'pv_brand': {
        'command_callback': self.set_pv_brand,
        'config_topic': f'{DISCOVERY_PREFIX}/select/{node_id}/pv_brand/config',
        'config': {
          'device': device_info,
          'name': 'SolarFlow PV Brand',
          'object_id': 'solarflow_pv_brand',
          'state_topic': 'solarflow/pv_brand/state',
          'command_topic': 'solarflow/pv_brand/set',
          'unique_id': f'{node_id}_pv_brand',
          'icon': 'mdi:domain',
          'options': list(PV_BRANDS.keys()),
        }
      },
    }

    for i, serial in enumerate(self.battery_packs):
        pack_name = f'pack_{serial}'
        pack_soc = f'{pack_name}_soc'
        pack_state = f'{pack_name}_state'
        pack_temp = f'{pack_name}_temperature'
        pack_index = i + 1
        new_topics[pack_soc] = {
          'config_topic': f'{DISCOVERY_PREFIX}/sensor/{node_id}/{pack_soc}/config',
          'config': {
            'device': device_info,
            'device_class': 'battery',
            'unit_of_measurement': '%',
            'name': f'SolarFlow Battery Pack {pack_index}',
            'object_id': f'solarflow_{pack_soc}',
            'state_topic': f'solarflow/{pack_soc}/state',
            'unique_id': f'{node_id}_{pack_soc}',
          },
        }
        new_topics[pack_state] = {
          'config_topic': f'{DISCOVERY_PREFIX}/sensor/{node_id}/{pack_state}/config',
          'config': {
            'device': device_info,
            'name': f'SolarFlow Battery Pack {pack_index} State',
            'object_id': f'solarflow_{pack_state}',
            'state_topic': f'solarflow/{pack_state}/state',
            'unique_id': f'{node_id}_{pack_state}',
          },
        }
        new_topics[pack_temp] = {
          'config_topic': f'{DISCOVERY_PREFIX}/sensor/{node_id}/{pack_temp}/config',
          'config': {
            'device': device_info,
            'device_class': 'temperature',
            'unit_of_measurement': 'K',
            'name': f'SolarFlow Battery Pack {pack_index} Temperature',
            'object_id': f'solarflow_{pack_temp}',
            'state_topic': f'solarflow/{pack_temp}/state',
            'unique_id': f'{node_id}_{pack_temp}',
          },
        }

    new_command_topics = [info['config']['command_topic']
                          for info in self.topics.values()
                          if 'command_topic' in info['config']]
    for topic in new_command_topics:
      if topic not in self.command_topics:
        self.mqtt_subscribe(topic)
        self.command_topics.append(topic)

    for item, info in new_topics.items():
      if item not in self.topics:
        self.mqtt_publish(info['config_topic'], json.dumps(info['config']))
        if 'start_value' in info:
          self.mqtt_publish(info['config']['state_topic'], info['start_value'])
        self.topics[item] = info

    # Periodically run this again, performs this once right away
    self.periodic_request_all()

  def message_received(self, event_name: str, data, cb_args) -> None:
    topic = data['topic']
    if topic in self.topic_callbacks:
      try:
        parsed_payload = json.loads(data['payload'])
        self.topic_callbacks[topic](parsed_payload)
      except json.JSONDecodeError as e:
        payload = data['payload']
        self.log(f'Failed to decode JSON for "{payload}": {e}')

    elif topic in self.command_topics:
      self.command_received(topic, data['payload'])

    else:
      self.log(f'Received message for topic "{topic}", but no callback registered')

  def log_received(self, data) -> None:
    if 'log' not in data:
      return
    log = data['log']
    if self.device_serial is None:
      if 'sn' in log:
        self.device_serial = log['sn']
        self.log(f'Received serial {self.device_serial} via log')

  def firmware_report_received(self, data) -> None:
    if self.device_serial is None:
      self.device_serial = data['deviceSn']
      self.log(f'Received serial {self.device_serial} via firmware report')
    for module in data["modules"]:
      self.module_versions[module["module"]] = module["version"]

  def time_sync_received(self, data) -> None:
    reply = {
      'messageId': self.generate_message_id(),
      'timestamp': self.generate_timestamp(),
    }
    self.log('Received time-sync request, replying with current time')
    self.mqtt_publish(self.topic_name_for('time-sync/reply', command=True),
                      json.dumps(reply))

  def publish_state(self, item: str, state: Union[str, int]) -> None:
    self.cache[item] = state

    state_topic = self.state_topic(item)
    if state_topic is None:
      return

    self.mqtt_publish(state_topic, state)

  def properties_report_received(self, data) -> None:
    if 'properties' not in data:
      return

    properties = data['properties']

    calc_solar_overflow = False
    calc_state = False
 
    if 'electricLevel' in properties:
      self.publish_state('battery', properties['electricLevel'])

    if 'outputHomePower' in properties:
      self.publish_state('home_output_power', properties['outputHomePower'])

    if 'outputPackPower' in properties:
      output_power = properties['outputPackPower']
      self.publish_state('battery_output_power', output_power)
      calc_state = True

    if 'solarInputPower' in properties:
      self.publish_state('solar_input_power', properties['solarInputPower'])
      calc_solar_overflow = True
      calc_state = True

    # Have seen pvPower* keys in Hub1200, and solarPower* in Hub2000
    if 'pvPower1' in properties:
      self.publish_state('solar_input_1_power', properties['pvPower1'])

    if 'solarPower1' in properties:
      self.publish_state('solar_input_1_power', properties['solarPower1'])

    if 'pvPower2' in properties:
      self.publish_state('solar_input_2_power', properties['pvPower2'])

    if 'solarPower2' in properties:
      self.publish_state('solar_input_2_power', properties['solarPower2'])

    if 'packInputPower' in properties:
      self.publish_state('battery_input_power', properties['packInputPower'])
      calc_solar_overflow = True
      calc_state = True

    if 'passMode' in properties:
      bypass_mode = properties['passMode']
      bypass_option = list(BYPASS_MODES.keys())[list(BYPASS_MODES.values()).index(bypass_mode)]
      self.publish_state('bypass_mode', bypass_option)

    # The solar input that we have more than what we store in the battery is
    # our overflow. Calculating this is useful for the energy dashboard.
    if calc_solar_overflow and 'solar_input_power' in self.cache and 'battery_input_power' in self.cache:
      self.publish_state('solar_overflow_power',
                         max(self.cache['solar_input_power'] - self.cache['battery_output_power'], 0))

    if calc_state:
      charge_power = 0
      discharge_power = 0
      solar_power = 0
      if 'battery_input_power' in self.cache:
        discharge_power = self.cache['battery_input_power']
      if 'battery_output_power' in self.cache:
        charge_power = self.cache['battery_output_power']
      if 'solar_input_power' in self.cache:
        solar_power = self.cache['solar_input_power']

      state = 'idle'
      if discharge_power > 0:
        state = 'discharging'
      elif charge_power > 0:
        state = 'charging'
      elif solar_power > 0:
        state = 'solar_passthrough'

      self.publish_state('state', state)
 
    if 'outputLimit' in properties:
      self.publish_state('output_limit', properties['outputLimit'])

    if 'inverseMaxPower' in properties:
      self.publish_state('max_inverter_input', properties['inverseMaxPower'])

    if 'minSoc' in properties:
      self.publish_state('min_soc', properties['minSoc'] / 10)

    if 'socSet' in properties:
      self.publish_state('max_soc', properties['socSet'] / 10)

    if 'remainOutTime' in properties:
      self.publish_state('battery_runtime', properties['remainOutTime'])

    if 'remainInputTime' in properties:
      self.publish_state('battery_charge_time', properties['remainInputTime'])

    if 'pvBrand' in properties:
      pv_brand = properties['pvBrand']
      option = list(PV_BRANDS.keys())[list(PV_BRANDS.values()).index(pv_brand)]
      self.publish_state('pv_brand', option)

    if 'buzzerSwitch' in properties:
      buzzer_switch = properties['buzzerSwitch']
      state = 'OFF'
      if buzzer_switch == 1:
        state = 'ON'
      self.publish_state('buzzer_switch', state)

    if 'packNum' in properties:
      # This message has the canonical enumeration of battery packs
      if 'packData' in data:
        pack_data = data['packData']
        self.battery_packs = [pack['sn'] for pack in pack_data]

        # After getting battery info, re-schedule sending discovery info
        self.run_in(self.try_send_discovery, SEND_DISCOVERY_INTERVAL_SEC)

    if 'packData' in data:
      pack_data = data['packData']

      for pack in pack_data:
        pack_serial = pack['sn'] 
        if pack_serial in self.battery_packs:
          pack_name = f'pack_{pack["sn"]}'
          if 'socLevel' in pack:
            self.publish_state(f'{pack_name}_soc', pack['socLevel'])
 
          if 'state' in pack:
            pack_state = pack['state']
            state = 'unknown'
            if pack_state == 0:
              state = 'standby'
            elif pack_state == 1:
              state = 'charging'
            elif pack_state == 2:
              state = 'discharging'
            self.publish_state(f'{pack_name}_state', state)
 
          if 'maxTemp' in pack:
            pack_temp = pack['maxTemp'] / 10.
            self.publish_state(f'{pack_name}_temperature', pack_temp)

  def command_received(self, topic: str, data) -> None:
    for info in self.topics.values():
      if 'command_topic' in info['config']:
        if info['config']['command_topic'] == topic:
          if 'command_callback' in info:
            info['command_callback'](data)

  def set_min_soc(self, state: str) -> None:
    state_int = int(state)
    if state_int < 0 or state_int > 30:
      self.log(f'Received invalid min soc {state_int} (required 0 <= S <= 30)')
      return
    args = {
      'properties': {
        'minSoc': state_int * 10,
      },
    }
    self.mqtt_publish(self.topic_name_for('properties/write', command=True),
                      self.create_request(args))

  def set_max_soc(self, state: str) -> None:
    state_int = int(state)
    if state_int < 70 or state_int > 100:
      self.log(f'Received invalid state {state_int} (required 70 <= S <= 100)')
      return
    args = {
      'properties': {
        'socSet': state_int * 10,
      },
    }
    self.mqtt_publish(self.topic_name_for('properties/write', command=True),
                      self.create_request(args))

  def set_max_inverter_input(self, state: str) -> None:
    state_int = int(state)
    if state_int < 0 or state_int > 1200:
      self.log(f'Received invalid state {state_int} (required 0 <= S <= 1200)')
      return
    if 'pv_brand' not in self.cache:
      self.log('PV inverter brand unknown, cannot set max inverter input')
      return

    args = {
      'properties': {
        'inverseMaxPower': state_int,
        'pvBrand': PV_BRANDS[self.cache['pv_brand']],
      },
    }
    self.mqtt_publish(self.topic_name_for('properties/write', command=True),
                      self.create_request(args))

  def set_pv_brand(self, state: str) -> None:
    if state not in PV_BRANDS:
      self.log(f'Received invalid pv brand {state}, not in list of known brands ({", ".join(PV_BRANDS)})')
      return
    if 'max_inverter_input' not in self.cache:
      self.log(f'Cannot set PV brand, max inverter input still unknown')
      return

    args = {
      'properties': {
        'inverseMaxPower': self.cache['max_inverter_input'],
        'pvBrand': PV_BRANDS[state],
      },
    }
    self.mqtt_publish(self.topic_name_for('properties/write', command=True),
                      self.create_request(args))

  def set_bypass_mode(self, state: str) -> None:
    if state not in BYPASS_MODES:
      self.log(f'Received invalid bypass mode {state}, not in list of known modes ({", ".join(BYPASS_MODES)})')
      return

    args = {
      'properties': {
        'passMode': BYPASS_MODES[state],
      },
    }
    self.mqtt_publish(self.topic_name_for('properties/write', command=True),
                      self.create_request(args))

  def set_output_limit(self, state: str) -> None:
    if 'max_inverter_input' not in self.cache:
      self.log('Max inverter input not yet known, cannot set output limit')
      return
    state_int = int(state)
    if state_int < 0 or state_int > self.cache['max_inverter_input']:
      self.log(f'Received invalid output limit {state_int} (required 0 <= S <= {self.cache["max_inverter_input"]})')
      return
    args = {
      'properties': {
        'outputLimit': state_int,
      },
    }
    self.mqtt_publish(self.topic_name_for('properties/write', command=True),
                      self.create_request(args))

  def set_buzzer_switch(self, state: str) -> None:
    state_int = 0
    if state == 'ON':
      state_int = 1
    args = {
      'properties': {
        'buzzerSwitch': state_int,
      },
    }
    self.mqtt_publish(self.topic_name_for('properties/write', command=True),
                      self.create_request(args))
