

import sys
import os
from datetime import datetime
import pytest
from unittest.mock import MagicMock

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from main import PeakEfficiency, ClimateState, ZoneSummary, DailySummary

os.environ["HASS_URL"] = "http://mock-hass-url"
os.environ["HASS_TOKEN"] = "mock-token"

class MockPeakEfficiency(PeakEfficiency):
    def __init__(self):
        # Don't call Hass.__init__ or AppDaemon-related setup
        super().__init__()
        object.__setattr__(self, "log", print)
        object.__setattr__(self, "get_state", MagicMock(side_effect=self._mock_get_state))
        object.__setattr__(self, "call_service", MagicMock())
        object.__setattr__(self, "listen_state", MagicMock())
        object.__setattr__(self, "listen_event", MagicMock())
        object.__setattr__(self, "run_daily", MagicMock())
        object.__setattr__(self, "cancel_timer", MagicMock())
        object.__setattr__(self, "args", {})
        self.heat_to_temp = 19.5
        self.restore_temp = 13
        self.summary = DailySummary.load("test:summary")
        self.active_queue = []

    def _assert_api_running(self):
        # Override to do nothing in the mock
        pass        

    def _mock_get_state(self, entity, attribute=None):
        if attribute == "current_temperature":
            return 18.3
        return "heat"

    def save_climate_state(self, state: ClimateState):
        self.cache["zone_state"] = state.to_json()

    def get_climate_state(self, clear_after_reading=True):
        raw = self.cache.get("zone_state")
        if not raw:
            return None
        if clear_after_reading:
            del self.cache["zone_state"]
        return ClimateState.from_json(raw)

@pytest.fixture
def app():
    app = MockPeakEfficiency()
    app.summary.clear()
    return app

def test_process_next_zone(app):
    app.active_queue = ["climate.garage"]
    app.process_next_zone()

    assert "climate.garage" in app.summary.zones
    zone = app.summary.zones["climate.garage"]
    assert zone.start_temp == 18.3
    assert zone.outside_temp == 18.3
    assert zone.duration == 1200

    app.call_service.assert_called_with(
        "climate/set_temperature",
        entity_id="climate.garage",
        temperature=app.heat_to_temp,
    )

def test_stop_heat_soak(app):
    # Simulate starting the zone
    app.active_queue = ["climate.garage"]
    app.process_next_zone()

    # Update current temperature
    app.get_state = MagicMock(side_effect=lambda entity, attribute=None: "20.1" if attribute == "current_temperature" else "heat")

    # Simulate stop
    app.stop_heat_soak(None, None, None)

    zone = app.summary.zones["climate.garage"]
    assert zone.end_temp == "20.1"

    app.call_service.assert_called_with(
        "climate/set_temperature",
        entity_id="climate.garage",
        temperature=app.restore_temp,
    )

