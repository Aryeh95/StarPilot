from types import SimpleNamespace

from cereal import car, log
from opendbc.car import DT_CTRL
from opendbc.car.hyundai.values import CAR as HYUNDAI_CAR

from openpilot.selfdrive.car.car_specific import CarSpecificEvents
from openpilot.selfdrive.car.cruise_state import should_cancel_stock_cruise, should_flag_cruise_mismatch


def make_cp(brand="hyundai", op_long=True, pcm_cruise=False):
  cp = car.CarParams.new_message()
  cp.brand = brand
  cp.openpilotLongitudinalControl = op_long
  cp.pcmCruise = pcm_cruise
  return cp


def test_hyundai_openpilot_long_does_not_cancel_active_acc_req_feedback():
  cp = make_cp()

  assert not should_cancel_stock_cruise(cp, cruise_enabled=True, controls_enabled=True)
  assert not should_flag_cruise_mismatch(cp, cruise_enabled=True, controls_enabled=True, effective_pcm_cruise=False)


def test_hyundai_openpilot_long_still_flags_cruise_when_controls_disabled():
  cp = make_cp()

  assert should_cancel_stock_cruise(cp, cruise_enabled=True, controls_enabled=False)
  assert should_flag_cruise_mismatch(cp, cruise_enabled=True, controls_enabled=False, effective_pcm_cruise=False)


def test_non_hyundai_openpilot_long_behavior_is_unchanged():
  cp = make_cp(brand="toyota")

  assert should_cancel_stock_cruise(cp, cruise_enabled=True, controls_enabled=True)
  assert should_flag_cruise_mismatch(cp, cruise_enabled=True, controls_enabled=True, effective_pcm_cruise=False)


def test_pcm_cruise_behavior_is_unchanged():
  cp = make_cp(op_long=False, pcm_cruise=True)

  assert not should_cancel_stock_cruise(cp, cruise_enabled=True, controls_enabled=True)
  assert should_cancel_stock_cruise(cp, cruise_enabled=True, controls_enabled=False)
  assert not should_flag_cruise_mismatch(cp, cruise_enabled=True, controls_enabled=True, effective_pcm_cruise=True)
  assert should_flag_cruise_mismatch(cp, cruise_enabled=True, controls_enabled=False, effective_pcm_cruise=True)


def test_kia_ray_ev_allows_stock_cruise_to_enable_controls():
  for candidate, ignore_cruise_state in ((HYUNDAI_CAR.KIA_RAY_EV, True), (HYUNDAI_CAR.HYUNDAI_SONATA, False)):
    cp = SimpleNamespace(brand="hyundai", carFingerprint=candidate, flags=0)
    handler = CarSpecificEvents(cp)
    captured = {}
    handler.create_common_events = lambda *args, **kwargs: captured.update(kwargs)

    handler.update(SimpleNamespace(), SimpleNamespace(), SimpleNamespace())

    assert captured["pcm_enable"] is True
    assert captured["ignore_cruise_state"] is ignore_cruise_state


def test_override_hold_suppresses_temporary_steer_fault_alerts():
  cp = make_cp(brand="hyundai", op_long=False, pcm_cruise=True)
  handler = CarSpecificEvents(cp)
  cs_prev = car.CarState.new_message()
  cs = car.CarState.new_message()
  cs.gearShifter = car.CarState.GearShifter.drive
  cs.cruiseState.available = True
  cs.cruiseState.enabled = True
  cs.steeringPressed = True

  # Prime override_hold during steering override
  events = handler.create_common_events(cs, cs_prev, pcm_enable=False)
  assert log.OnroadEvent.EventName.steerOverride in events.names

  # Release steering slightly before temporary steer fault clears: silent alert is suppressed
  cs.steeringPressed = False
  cs.steerFaultTemporary = True
  events = handler.create_common_events(cs, cs_prev, pcm_enable=False)
  assert log.OnroadEvent.EventName.steerTempUnavailableSilent not in events.names
  assert log.OnroadEvent.EventName.steerTempUnavailable not in events.names

  # Once override_hold expires, a lingering fault stays silent via the latch (no disengage)
  handler.override_hold = 0
  events = handler.create_common_events(cs, cs_prev, pcm_enable=False)
  assert log.OnroadEvent.EventName.steerTempUnavailableSilent in events.names

  # Fresh fault with no prior override still raises the normal steerTempUnavailable warning
  fresh_handler = CarSpecificEvents(cp)
  fresh_handler.steering_unpressed = int(2.0 / DT_CTRL)
  fresh_handler.silent_steer_warning = False
  events = fresh_handler.create_common_events(cs, cs_prev, pcm_enable=False)
  assert log.OnroadEvent.EventName.steerTempUnavailable in events.names
