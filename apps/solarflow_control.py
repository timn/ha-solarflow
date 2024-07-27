# Copyright (C) 2023-2024 Tim Niemueller
#
# Controller to set SolarFlow battery output automatically given an overall
# house total (positive for consumption and negative for net production) by
# various criteria.

from typing import Any, Optional

from appdaemon.entity import Entity
import hassapi
import abc
from datetime import datetime, time
import sys

SOLARFLOW_BATTERY_SOC = "sensor.solarflow_battery"
HOUSE_POWER = "sensor.powermeter_house_active_power"
SOLARFLOW_TO_BATTERY = "sensor.solarflow_battery_input_power"
SOLARFLOW_FROM_BATTERY = "sensor.solarflow_battery_output_power"
SOLARFLOW_OUTPUT_LIMIT = "number.solarflow_output_limit"
SOLARFLOW_MIN_SOC = "number.solarflow_min_soc"

CONTROLLER_SWITCH_NAME = "switch.solarflow_control"

DEFAULT_MAX_OUTPUT = 500.
LOOP_PERIOD_SEC = 30

class Controller(abc.ABC):
  """Base class for set point controller.

  A controller needs to implement compute().
  """
  solarflow_control: 'SolarFlowControl'

  def __init__(self, solarflow_control: 'SolarFlowControl'):
    self.solarflow_control = solarflow_control

  @abc.abstractmethod
  def compute(self) -> Optional[float]:
    """Computes the next set point.

    This is called periodically by the SolarFlowControl main program. It should
    return the next desired set point for the SolarFlow output limit. It can
    use value retrieved from HomeAssistant.

    Returns:
      None if no new set point was calculated (currently set point will remain
      effective), or a float number of the new limit. The limit does not need to
      account for min and max output values, the caller will take care of that.
    """
    return None

  def get_value(self, entity: str) -> float:
    """Gets a numeric value from the set of supported input states (see above).

    Args:
      entity: entity ID to query

    Returns:
      Numeric value, returns 0 if value could not be received.
    """
    return self.solarflow_control.get_value(entity)


class AlwaysZero(Controller):
  """Example controller that always returns 0 as set point.

  It can be used to charge as much as possible and when the battery is fully
  charged SolarFlow will start feeding solar power to the inverter.
  """

  def __init__(self, solarflow_control: 'SolarFlowControl'):
    Controller.__init__(self, solarflow_control)

  def compute(self) -> Optional[float]:
    return 0.0


class MinimizeGrid(Controller):
  """Controller that aggressively minimizes usage from grid.

  This will use the max possible value for battery use. It relies on max output
  clamping by the caller (it will return arbitrarily large numbers based on total
  grid consumption).
  """

  # Min absolute difference of current output limit to actual consumption to
  # increase output
  CONSUMPTION_THRESHOLD = 5.

  def __init__(self, solarflow_control: 'SolarFlowControl'):
    Controller.__init__(self, solarflow_control)

  def compute(self) -> Optional[float]:
    # We are feeding energy to the grid
    if self.get_value(HOUSE_POWER) < 0 and self.get_value(SOLARFLOW_OUTPUT_LIMIT) > 0:
      return self.get_value(SOLARFLOW_OUTPUT_LIMIT) + self.get_value(HOUSE_POWER)

    # We are drawing energy from the grid, try to increase battery output
    if self.get_value(HOUSE_POWER) > self.CONSUMPTION_THRESHOLD:
      return self.get_value(SOLARFLOW_OUTPUT_LIMIT) + self.get_value(HOUSE_POWER)

    return None

class NightUsage(Controller):
  """Controller to charge during the day and supply during the night.

  This will ramp down some time before a configurable morning cut-off. The ramp
  down is bound to solar power being available and will only reduce supply until
  it hits zero. Then it stays at zero during the day (when the battery is fully
  charged the SolarFlow will start feeding solar power automatically). In the
  afternoon (configurable start time) it will ramp-up slowly as grid energy
  increases.
  """

  morning_cutoff_time: time
  evening_rampup_time: time

  # Min absolute difference of current output limit to actual consumption to
  # increase output
  CONSUMPTION_THRESHOLD = 10.

  def __init__(self, solarflow_control: 'SolarFlowControl'):
    Controller.__init__(self, solarflow_control)
    self.morning_cutoff_time = time.fromisoformat(solarflow_control.args['morning_cutoff_time'])
    self.evening_rampup_time = time.fromisoformat(solarflow_control.args['evening_rampup_time'])

  def compute(self) -> Optional[float]:
    now = datetime.now().time()

    # That's day time when we don't want to run on battery
    if now >= self.morning_cutoff_time and now < self.evening_rampup_time:
      if self.get_value(SOLARFLOW_OUTPUT_LIMIT) != 0.:
        return 0.
      else:
        return None

    # Try to keep us around zero:
    # if we're feeding to the grid, house power will be less than zero and we
    # will decrease battery output. If we're consuming it's positive and we
    # increase. Clamping to max_output happens outside.
    # On over-production we always immediately reduce, on minor consumption we
    # observe a threshold.
    house_power = self.get_value(HOUSE_POWER)
    if house_power < 0 or house_power > self.CONSUMPTION_THRESHOLD:
      return self.get_value(SOLARFLOW_OUTPUT_LIMIT) + house_power

    return None

class SolarFlowControl(hassapi.Hass):
  """SolarFlow automatic controller.

  This class takes control of SolarFlow and uses a configurable set point
  controller to determine the next value to set.

  Values are automatically clamped to the range [0, max_output], where
  max_output is configurable in apps.yaml.
  """

  topic_prefix: str
  entities: dict[str, Entity]
  loop_handle: str
  controller: Controller

  max_output: float
  
  def initialize(self) -> None:

    if 'max_output' in self.args:
      self.max_output = float(self.args['max_output'])
    else:
      self.max_output = DEFAULT_MAX_OUTPUT

    controller_class_name = self.args['controller_class']
    controller_class = getattr(sys.modules[__name__], controller_class_name, None)
    if controller_class is None:
      raise ImportError(f'Failed to load controller class {controller_class}')
    self.controller = controller_class(self)

    self.controller_switch = self.get_entity(CONTROLLER_SWITCH_NAME)
    self.controller_switch.set_state(state="on",
                                     attributes={'friendly_name': 'SolarFlow Automatic Controller',
                                                 'icon': 'mdi:car-cruise-control'})
    self.service_callback_handle = self.listen_event(self.call_service_callback, event="call_service")

    self.entities = {}
    self.add_sensor(SOLARFLOW_BATTERY_SOC)
    self.add_sensor(SOLARFLOW_TO_BATTERY)
    self.add_sensor(SOLARFLOW_FROM_BATTERY)
    self.add_sensor(HOUSE_POWER)
    self.add_sensor(SOLARFLOW_OUTPUT_LIMIT)
    self.add_sensor(SOLARFLOW_MIN_SOC)
    self.loop_handle = self.run_every(self.control_loop, "now", LOOP_PERIOD_SEC)

  def terminate(self) -> None:
    if self.loop_handle: self.cancel_timer(self.loop_handle)
    if self.service_callback_handle: self.cancel_listen_event(self.service_callback_handle)

  def call_service_callback(self, event_name: str, data, cb_args = {}) -> None:
    if event_name != 'call_service':
      return
    if 'entity_id' not in data['service_data']:
      return

    if data['service_data']['entity_id'] == CONTROLLER_SWITCH_NAME:
      if data['service'] == 'turn_off':
        self.controller_switch.set_state(state='off')
      elif data['service'] == 'turn_on':
        self.controller_switch.set_state(state='on')

  def control_loop(self, cb_args = {}) -> None:
    if self.controller_switch.get_state() == 'off':
      self.log("Controller disabled", level="INFO")
      return

    next_value = None

    # Batteries depleted
    if int(self.get_value(SOLARFLOW_BATTERY_SOC)) <= int(self.get_value(SOLARFLOW_MIN_SOC)):
      if self.get_value(SOLARFLOW_OUTPUT_LIMIT) > 0:
        self.log("Battery depleted, setting output limit to zero", level="INFO")
        next_value = 0.
    else:
      next_value = self.controller.compute()

    if next_value is not None:
      next_value = min(self.max_output, next_value)
      next_value = max(0, next_value)
      self.entities[SOLARFLOW_OUTPUT_LIMIT].call_service('set_value', value=next_value)
    # else:
    #   self.log("No new value from controller", level="INFO")

  def get_value(self, entity: str) -> float:
    state = self.entities[entity].get_state()
    if state is None or state == "unavailable": return 0.
    return float(state)

  def add_sensor(self, entity: str) -> None:
    #self.handles[entity] = self.listen_state(self.update_sensor, entity, immediate=True)
    entity_obj = self.get_entity(entity)
    self.entities[entity] = entity_obj
