

import sys
import os
from datetime import datetime
import pytest
from unittest.mock import MagicMock
import hassapi as hass
from pydantic import BaseModel

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from main import PeakEfficiency, ZoneSummary, DailySummary

from main import (
    MANUAL_START,
    DRY_RUN,
    OUTDOOR_TEMPERATURE_SENSOR,
    AWAY_TARGET_TEMP,
    AWAY_PEAK_HEAT_TO_TEMP,
    AWAY_MODE_ENABLED,
    PEAK_EFFICIENCY_DISABLED,
    CACHE_PATH
)

# # delete cache directory and all files
# if os.path.exists(CACHE_PATH):
#     for file in os.listdir(CACHE_PATH):
#         os.remove(os.path.join(CACHE_PATH, file))
#     os.rmdir(CACHE_PATH)

os.environ["HASS_URL"] = "http://mock-hass-url"
os.environ["HASS_TOKEN"] = "mock-token"

class MockPeakEfficiency(PeakEfficiency):
    def __init__(self):
        # Don't call Hass.__init__ or AppDaemon-related setup
        object.__setattr__(self, "log", MagicMock(side_effect=self._mock_print))
        object.__setattr__(self, "get_state", MagicMock(side_effect=self._mock_get_state))
        object.__setattr__(self, "call_service", MagicMock())
        object.__setattr__(self, "listen_state", MagicMock())
        object.__setattr__(self, "cancel_listen_state", MagicMock())
        object.__setattr__(self, "listen_event", MagicMock())
        object.__setattr__(self, "run_daily", MagicMock(side_effect=self._mock_run_daily))
        object.__setattr__(self, "run_in", MagicMock(side_effect=self._mock_run_in))
        object.__setattr__(self, "cancel_timer", MagicMock())
        object.__setattr__(self, "args", {"latitude": 50.88171971069347, "longitude": -119.89710569337053})
        object.__setattr__(self, "run_daily_jobs", [])
        object.__setattr__(self, "run_in_jobs", [])
        object.__setattr__(self, "_temperature_cycle", [14, 14.5, 15])
        object.__setattr__(self, "_temperature_index", 0)

    def _assert_api_running(self):
        # Override to do nothing in the mock
        pass

    def _mock_print(self, message, level="INFO"):
        print(f"{level}: {message}")

    def _mock_get_state(self, entity, attribute=None):
       
        if entity == MANUAL_START:
            return "on"
        elif entity == DRY_RUN:
            return "off"
        elif entity == OUTDOOR_TEMPERATURE_SENSOR:
            return "15.0"
        elif entity == AWAY_TARGET_TEMP:
            return "13.5"
        elif entity == AWAY_PEAK_HEAT_TO_TEMP:
            return "20.0"
        elif entity == AWAY_MODE_ENABLED:
            return "on"
        elif entity == PEAK_EFFICIENCY_DISABLED:
            return "off"
        elif entity.startswith("climate.") and attribute == "current_temperature":
            value = self._temperature_cycle[self._temperature_index]
            self._temperature_index = (self._temperature_index + 1) % len(self._temperature_cycle)
            return value
        return "heat"

    def _mock_run_daily(self, func, time, **kwargs):
        #add the scheduled task to the list
        self.run_daily_jobs.append((func, time, kwargs))

    def _mock_run_in(self, func, delay, **kwargs):
        #add the scheduled task to the list
        print(f"Scheduling {func.__name__} to run in {delay} seconds with args {kwargs}")
        self.run_in_jobs.append((func, delay, kwargs))

@pytest.fixture
def app():
    app = MockPeakEfficiency()
    app.initialize()
    return app

def test_full_day(app):

    app.start_heat_soak()
    while app.run_in_jobs:
        job = app.run_in_jobs.pop(0)  # Remove the job from the list
        job_func, job_delay, job_kwargs = job  # Unpack the tuple
        job_func(job_kwargs)  # Call the function with the kwargs

    app.finalize_day()

def test_resume_after_restart(app):

    app.start_heat_soak()

# Simulate a restart by clearing the run_in_jobs list
    
    print("#################################")
    print("Simulating restart...")
    print("#################################")

    app = MockPeakEfficiency()
    app.initialize()

    while app.run_in_jobs:
        job = app.run_in_jobs.pop(0)  # Remove the job from the list
        job_func, job_delay, job_kwargs = job  # Unpack the tuple
        job_func(job_kwargs)  # Call the function with the kwargs

    app.finalize_day()    


# def test_stop_heat_soak(app):
#     # Simulate starting the zone
#     app.active_queue = ["climate.garage"]
#     app.process_next_zone()

#     # Update current temperature
#     app.get_state = MagicMock(side_effect=lambda entity, attribute=None: "20.1" if attribute == "current_temperature" else "heat")

#     # Simulate stop
#     app.stop_heat_soak(None, None, None)

#     zone = app.summary.zones["climate.garage"]
#     assert zone.end_temp == "20.1"

#     app.call_service.assert_called_with(
#         "climate/set_temperature",
#         entity_id="climate.garage",
#         temperature=app.restore_temp,
#     )

