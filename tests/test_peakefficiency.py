import unittest
from unittest.mock import MagicMock, patch
from appdaemon_testing.pytest import automation_fixture
from apps.PeakEfficiency import PeakEfficiency

class TestPeakEfficiency(unittest.TestCase):
  
    @automation_fixture(PeakEfficiency, args={"some_arg": "value"})
    def PeakEfficiency():
        pass  

    def setUp(self, mock_hass):
        # Create an instance of the PeakEfficiency class
        self.app = PeakEfficiency()
        self.app.log = MagicMock()
        self.app.get_state = MagicMock()
        self.app.call_service = MagicMock()
        self.app.run_in = MagicMock()

        # Mock heating durations
        self.app.heat_durations = {
            "climate.main_floor": 40 * 60,
            "climate.master_bedroom": 20 * 60,
            "climate.basement_master": 20 * 60,
            "climate.basement_bunk_rooms": 30 * 60,
            "climate.ski_room": 10 * 60
        }

        # Mock restore temperature and heat-to temperature
        self.app.restore_temp = 13
        self.app.heat_to_temp = 19.5

    def test_call_service_set_temperature(self):
        # Mock the state of climate entities
        self.app.get_state.side_effect = lambda entity_id, attribute=None: "heat" if entity_id in self.app.heat_durations else None

        # Mock the active queue
        self.app.active_queue = ["climate.main_floor"]

        # Call process_next_climate
        self.app.process_next_climate()

        # Assert that call_service was called with "climate/set_temperature"
        self.app.call_service.assert_called_with("climate/set_temperature", entity_id="climate.main_floor", temperature=19.5)

    def test_no_climate_entities_in_heat_mode(self):
        # Mock the state of climate entities to be "off"
        self.app.get_state.side_effect = lambda entity_id, attribute=None: "off"

        # Call start_override
        self.app.start_override()

        # Assert that no entities were added to the active queue
        self.assertEqual(len(self.app.active_queue), 0)

        # Assert that log was called with the appropriate message
        self.app.log.assert_called_with("No climate entities in heat mode — nothing to do.")

    def test_restore_temperature(self):
        # Mock kwargs for restore_temperature
        kwargs = {
            "climate": "climate.main_floor",
            "outside_temp": 5,
            "start_temp": 18
        }

        # Call restore_temperature
        self.app.restore_temperature(kwargs)

        # Assert that call_service was called to restore the temperature
        self.app.call_service.assert_called_with("climate/set_temperature", entity_id="climate.main_floor", temperature=13)

        # Assert that log was called with the appropriate message
        self.app.log.assert_called_with(
            "Restored climate.main_floor to 13C | Outside: 5C | Start: 18C | End: NoneC"
        )

    def test_dry_run_mode(self):
        # Mock dry run mode to be "on"
        self.app.get_state.side_effect = lambda entity_id, attribute=None: "on" if entity_id == "input_boolean.peak_efficiency_dry_run" else "heat"

        # Mock the active queue
        self.app.active_queue = ["climate.main_floor"]

        # Call process_next_climate
        self.app.process_next_climate()

        # Assert that call_service was not called
        self.app.call_service.assert_not_called()

        # Assert that log was called with the appropriate message
        self.app.log.assert_called_with("climate.main_floor: Not modifying temperature as Dry Run mode is enabled")

if __name__ == "__main__":
    unittest.main()