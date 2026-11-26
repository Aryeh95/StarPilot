from types import SimpleNamespace

import numpy as np
import pytest

import openpilot.selfdrive.ui.onroad.model_renderer as model_renderer


class _FakeSubMaster:
  def __init__(self, plan):
    self.recv_frame = {"starpilotPlan": 1}
    self._plan = plan

  def __getitem__(self, key):
    assert key == "starpilotPlan"
    return self._plan


class _FakeParams:
  def __init__(self, *, hide_lead_marker=False):
    self.hide_lead_marker = hide_lead_marker

  def get(self, key):
    return b"1" if key == "HideLeadMarker" else None

  def get_bool(self, key):
    assert key == "HideLeadMarker"
    return self.hide_lead_marker


def test_lead_indicator_renders_without_longitudinal_control():
  renderer = object.__new__(model_renderer.ModelRenderer)
  renderer._params = _FakeParams()
  renderer._longitudinal_control = False
  renderer._lead_info_enabled = False

  assert renderer._should_render_lead_indicator(SimpleNamespace())


def test_lead_indicator_still_honors_visibility_setting():
  renderer = object.__new__(model_renderer.ModelRenderer)
  renderer._params = _FakeParams(hide_lead_marker=True)

  assert not renderer._should_render_lead_indicator(SimpleNamespace())
  assert not renderer._should_render_lead_indicator(None)


@pytest.mark.parametrize(
  ("lane_width_left", "lane_width_right", "expected_side"),
  [
    (3.5, 0.0, 0),
    (0.0, 3.5, 1),
  ],
)
def test_adjacent_path_keeps_a_single_valid_side(monkeypatch, lane_width_left, lane_width_right, expected_side):
  fake_ui_state = SimpleNamespace(
    sm=_FakeSubMaster(SimpleNamespace(laneWidthLeft=lane_width_left, laneWidthRight=lane_width_right)),
    started_frame=0,
  )
  monkeypatch.setattr(model_renderer, "ui_state", fake_ui_state)

  renderer = object.__new__(model_renderer.ModelRenderer)
  left_outer = np.ones((2, 3), dtype=np.float32)
  left_inner = np.ones((2, 3), dtype=np.float32)
  right_inner = np.ones((2, 3), dtype=np.float32)
  right_outer = np.ones((2, 3), dtype=np.float32)
  renderer._lane_lines = [
    SimpleNamespace(raw_points=left_outer),
    SimpleNamespace(raw_points=left_inner),
    SimpleNamespace(raw_points=right_inner),
    SimpleNamespace(raw_points=right_outer),
  ]
  renderer._adjacent_path_vertices = [
    np.ones((4, 2), dtype=np.float32),
    np.ones((4, 2), dtype=np.float32),
  ]

  def fake_get_adjacent_path_polygon(line1, *_args):
    side = 0 if line1 is left_outer else 1
    return np.array([[side, 0], [side, 1], [side + 0.5, 1], [side + 0.5, 0]], dtype=np.float32)

  monkeypatch.setattr(renderer, "_get_adjacent_path_polygon", fake_get_adjacent_path_polygon)

  renderer._update_adjacent_paths(max_idx=1, max_distance=10.0)

  assert renderer._adjacent_path_vertices[expected_side].size >= 4
  assert renderer._adjacent_path_vertices[1 - expected_side].size == 0


def test_place_lead_label_rect_pushes_outward_and_drops_when_out_of_bounds():
  Rect = lambda x, y, w, h: SimpleNamespace(x=x, y=y, width=w, height=h)
  primary = Rect(900.0, 500.0, 200.0, 120.0)
  placed = [primary]

  # Left adjacent label colliding with primary is pushed outward to the left
  left_rect = Rect(820.0, 510.0, 140.0, 80.0)
  placed_left, displaced_left = model_renderer.ModelRenderer._place_lead_label_rect(
    left_rect, line_height=34.0, margin=8.5, bounds_x=0.0, bounds_w=2160.0, placed_rects=placed,
  )
  assert displaced_left is True
  assert placed_left.x + placed_left.width <= primary.x

  # Right label with no horizontal room falls back to dropping down a line
  right_edge = Rect(2000.0, 500.0, 150.0, 120.0)
  colliding = Rect(2020.0, 510.0, 140.0, 80.0)
  dropped, displaced_drop = model_renderer.ModelRenderer._place_lead_label_rect(
    colliding, line_height=34.0, margin=8.5, bounds_x=0.0, bounds_w=2160.0, placed_rects=[right_edge],
  )
  assert displaced_drop is True
  assert dropped.y > colliding.y


def test_lead_marker_style_sizing_and_minimal_glow():
  renderer = object.__new__(model_renderer.ModelRenderer)
  rect = SimpleNamespace(x=0.0, y=0.0, width=2160.0, height=1080.0)

  renderer._lead_marker_style = 0
  standard = renderer._update_lead_vehicle(20.0, 0.0, (1080.0, 540.0), rect)
  renderer._lead_marker_style = 1
  compact = renderer._update_lead_vehicle(20.0, 0.0, (1080.0, 540.0), rect)
  renderer._lead_marker_style = 2
  minimal = renderer._update_lead_vehicle(20.0, 0.0, (1080.0, 540.0), rect)

  assert len(standard.glow) == 3
  assert len(compact.glow) == 3
  assert minimal.glow == []
  assert len(minimal.chevron) == 3
  standard_w = standard.chevron[0][0] - standard.chevron[2][0]
  compact_w = compact.chevron[0][0] - compact.chevron[2][0]
  minimal_w = minimal.chevron[0][0] - minimal.chevron[2][0]
  assert compact_w < standard_w
  assert minimal_w < compact_w
