import platform
from types import SimpleNamespace

import cereal.messaging as messaging
from cereal import log
import pytest

from opendbc.car.toyota.values import CAR as TOYOTA
from opendbc.car.honda.values import CAR as HONDA
from openpilot.selfdrive.test.process_replay import replay_process_with_name
from openpilot.selfdrive.controls.lib.desire_helper import LaneChangeState
from openpilot.selfdrive.controls.radard import (
  DT_MDL,
  HONDA_BOSCH_A_RADAR_TS,
  RADAR_TO_CAMERA,
  KalmanParams,
  RadarD,
  Track,
  POST_STANDSTILL_RADAR_LEAD_PERSISTENCE_FRAMES,
  post_standstill_radar_lead_is_urgent,
  g90_low_speed_radar_lead_sane,
  g90_radar_lead_lateral_sane,
  has_slow_radar_tracks,
  is_bosch_a_radar_car,
  match_vision_to_track,
)


class TestLeads:
  @staticmethod
  def make_track(d_rel: float, y_rel: float, v_rel: float, cnt: int = 5):
    return SimpleNamespace(dRel=d_rel, yRel=y_rel, vRel=v_rel, cnt=cnt)

  @staticmethod
  def make_lead(x: float, y: float, v: float, x_std: float = 1.5, y_std: float = 0.3, v_std: float = 1.0):
    return SimpleNamespace(x=[x], y=[y], v=[v], xStd=[x_std], yStd=[y_std], vStd=[v_std])

  @staticmethod
  def make_model_data():
    model = log.ModelDataV2.new_message()
    model.init('laneLines', 4)
    model.meta.laneChangeState = 0
    model.meta.laneChangeDirection = 0
    return model

  def test_g90_radar_filters_side_tracks(self):
    side_track = SimpleNamespace(dRel=13.0, yRel=-10.38, cnt=10)
    centered_track = SimpleNamespace(dRel=10.8, yRel=-0.21, cnt=5)
    close_side_ghost = SimpleNamespace(dRel=2.2, yRel=2.41, cnt=10)
    close_centered_track = SimpleNamespace(dRel=2.2, yRel=1.2, cnt=10)
    far_low_speed_track = SimpleNamespace(dRel=15.5, yRel=0.58, cnt=5)

    assert not g90_radar_lead_lateral_sane(side_track)
    assert g90_radar_lead_lateral_sane(centered_track)
    assert not g90_radar_lead_lateral_sane(close_side_ghost)
    assert g90_radar_lead_lateral_sane(close_centered_track)
    assert g90_low_speed_radar_lead_sane(centered_track, 2.0)
    assert not g90_low_speed_radar_lead_sane(far_low_speed_track, 3.5)

  def test_match_vision_to_track_sticky_holds_preferred_track(self):
    v_ego = 21.2
    lead = self.make_lead(x=37.72, y=0.07, v=20.03)
    preferred_track = self.make_track(d_rel=46.95, y_rel=0.05, v_rel=-0.58, cnt=8)
    tracks = {3647: preferred_track}

    track = match_vision_to_track(
      v_ego,
      lead,
      self.make_model_data(),
      tracks,
      SimpleNamespace(human_lane_changes=False),
      preferred_track_id=3647,
    )

    assert track is preferred_track

  def test_match_vision_to_track_sticky_rejects_bad_preferred_track(self):
    v_ego = 21.2
    lead = self.make_lead(x=37.72, y=0.07, v=20.03)
    preferred_track = self.make_track(d_rel=58.0, y_rel=2.8, v_rel=-4.5, cnt=8)
    tracks = {3647: preferred_track}

    track = match_vision_to_track(
      v_ego,
      lead,
      self.make_model_data(),
      tracks,
      SimpleNamespace(human_lane_changes=False),
      preferred_track_id=3647,
    )

    assert track is None

  def test_bosch_a_radard_path_is_gated_to_verified_honda_radar(self):
    assert is_bosch_a_radar_car(SimpleNamespace(brand="honda", carFingerprint=HONDA.HONDA_CIVIC_BOSCH, radarUnavailable=False))
    assert not is_bosch_a_radar_car(SimpleNamespace(brand="toyota", carFingerprint=HONDA.HONDA_CIVIC_BOSCH, radarUnavailable=False))
    assert not is_bosch_a_radar_car(SimpleNamespace(brand="honda", carFingerprint=HONDA.HONDA_CIVIC_BOSCH, radarUnavailable=True))

  def test_bosch_a_and_legacy_lead_filter_timesteps_are_scoped(self):
    legacy = RadarD(radar_ts=0.06)
    bosch_a = RadarD(radar_ts=0.06, honda_bosch_a_radar=True)
    assert legacy.lead_prob_filters[0].dt == pytest.approx(0.06)
    assert bosch_a.lead_prob_filters[0].dt == pytest.approx(DT_MDL)
    assert bosch_a.kalman_params.A[0][1] == pytest.approx(HONDA_BOSCH_A_RADAR_TS)

  def test_slow_radar_frequency_relaxation_is_scoped(self):
    slow_radar = SimpleNamespace(radarTimeStepDEPRECATED=0.15, radarUnavailable=False)
    normal_radar = SimpleNamespace(radarTimeStepDEPRECATED=0.1, radarUnavailable=False)
    unavailable_radar = SimpleNamespace(radarTimeStepDEPRECATED=0.15, radarUnavailable=True)

    assert has_slow_radar_tracks(slow_radar)
    assert not has_slow_radar_tracks(normal_radar)
    assert not has_slow_radar_tracks(unavailable_radar)

  @staticmethod
  def make_radar_only_lead(track_id: int, d_rel: float = 8.0, v_rel: float = 0.0):
    return {
      "status": True,
      "radar": True,
      "modelProb": 0.0,
      "radarTrackId": track_id,
      "dRel": d_rel,
      "vRel": v_rel,
    }

  def test_post_standstill_radar_lead_requires_same_track_persistence(self):
    radar = RadarD()
    radar._post_standstill_gate_active = True
    lead = self.make_radar_only_lead(42)

    assert not radar._filter_post_standstill_lead(lead)["status"]
    assert not radar._filter_post_standstill_lead(lead)["status"]
    assert radar._filter_post_standstill_lead(lead)["status"]
    assert radar._post_standstill_candidate_frames == POST_STANDSTILL_RADAR_LEAD_PERSISTENCE_FRAMES

  def test_post_standstill_radar_lead_resets_for_new_track(self):
    radar = RadarD()
    radar._post_standstill_gate_active = True

    assert not radar._filter_post_standstill_lead(self.make_radar_only_lead(42))["status"]
    assert not radar._filter_post_standstill_lead(self.make_radar_only_lead(43))["status"]
    assert radar._post_standstill_candidate_frames == 1

  def test_post_standstill_gate_only_arms_after_no_lead_stop(self):
    radar = RadarD()
    radar._remember_post_standstill_state(standstill=True, lead_status=False)
    radar._prepare_post_standstill_gate(standstill=False)
    assert radar._post_standstill_gate_active

    radar = RadarD()
    radar._remember_post_standstill_state(standstill=True, lead_status=True)
    radar._prepare_post_standstill_gate(standstill=False)
    assert not radar._post_standstill_gate_active

  def test_post_standstill_model_lead_bypasses_gate(self):
    radar = RadarD()
    radar._post_standstill_gate_active = True
    lead = self.make_radar_only_lead(42)
    lead["modelProb"] = 0.9

    assert radar._filter_post_standstill_lead(lead)["status"]
    assert not radar._post_standstill_gate_active

  def test_post_standstill_urgent_radar_lead_bypasses_gate(self):
    radar = RadarD()
    radar._post_standstill_gate_active = True
    lead = self.make_radar_only_lead(42, d_rel=3.0, v_rel=-2.1)

    assert post_standstill_radar_lead_is_urgent(lead)
    assert radar._filter_post_standstill_lead(lead)["status"]
    assert not radar._post_standstill_gate_active

  @pytest.mark.skipif(platform.system() == "Darwin", reason="SocketEventHandle requires eventfd")
  def test_radar_fault(self):
    # if there's no radar-related can traffic, radard should either not respond or respond with an error
    # this is tightly coupled with underlying car radar_interface implementation, but it's a good sanity check
    def single_iter_pkg():
      # single iter package, with meaningless cans and empty carState/modelV2
      msgs = []
      for _ in range(500):
        can = messaging.new_message("can", 1)
        cs = messaging.new_message("carState")
        cp = messaging.new_message("carParams")
        msgs.append(can.as_reader())
        msgs.append(cs.as_reader())
        msgs.append(cp.as_reader())
      model = messaging.new_message("modelV2")
      msgs.append(model.as_reader())

      return msgs

    msgs = [m for _ in range(3) for m in single_iter_pkg()]
    out = replay_process_with_name("card", msgs, fingerprint=TOYOTA.TOYOTA_COROLLA_TSS2)
    states = [m for m in out if m.which() == "liveTracks"]
    failures = [not state.valid for state in states]

    assert len(states) == 0 or all(failures)

  def test_potential_adjacent_lead_model_frame_offset_and_lane_change_cache(self):
    model = log.ModelDataV2.new_message()
    model.init('laneLines', 4)
    xs = [0.0, 10.0, 20.0, 30.0]
    model.position.x = xs
    # Curve lane lines sharply right at x > 20 so radar-frame vs model-frame assignment differs
    for idx, ys in enumerate((
      [-5.5, -5.5, -5.5, -5.5],
      [-1.8, -1.8, -3.5, -3.5],
      [1.8, 1.8, 0.2, 0.2],
      [5.5, 5.5, 4.0, 4.0],
    )):
      model.laneLines[idx].x = xs
      model.laneLines[idx].y = ys

    track = Track(7, 20.0, KalmanParams(DT_MDL))
    # At dRel = 19.0, model_distance = 19.0 + RADAR_TO_CAMERA > 20.0 where laneLines[2] is 0.2 (so -yRel=1.0 is right lane)
    track.update(d_rel=19.0, y_rel=-1.0, v_rel=0.0, v_lead=20.0, measured=True)
    assert RADAR_TO_CAMERA > 1.0
    assert track.potential_adjacent_lead(left=False, standstill=False, model_data=model)
    assert track.potential_adjacent_lead(left=False, standstill=True, model_data=model)
    assert not track.potential_adjacent_lead(left=True, standstill=False, model_data=model)
    assert track.leadRight
    assert not track.leadLeft

    # Stopped adjacent tracks stay filtered even at ego standstill
    slow_track = Track(8, 0.2, KalmanParams(DT_MDL))
    slow_track.update(d_rel=19.0, y_rel=-1.0, v_rel=-0.2, v_lead=0.2, measured=True)
    assert not slow_track.potential_adjacent_lead(left=False, standstill=True, model_data=model)

    # Freeze cached leadLeft/leadRight while laneChangeStarting
    model.meta.laneChangeState = LaneChangeState.laneChangeStarting
    track.update(d_rel=5.0, y_rel=0.0, v_rel=0.0, v_lead=20.0, measured=True)
    assert track.potential_adjacent_lead(left=False, standstill=False, model_data=model)
    assert not track.potential_adjacent_lead(left=True, standstill=False, model_data=model)

    # Resumes updating and clears cached flags once laneChangeStarting ends or track slows down
    model.meta.laneChangeState = LaneChangeState.off
    assert not track.potential_adjacent_lead(left=False, standstill=False, model_data=model)
    assert not track.leadRight
