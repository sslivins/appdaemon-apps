from datetime import time
import hassapi as hass
from datetime import timedelta
import json

DEFAULT_HEATING_DURATION = 20 * 60  # Default heating duration in seconds
DEFAULT_PEAK_HEAT_TEMP = 19.5  # Default peak heating temperature in Celsius
DEFAULT_AWAY_MODE_TEMP = 13  # Default away mode temperature in Celsius

class PeakEfficiency(hass.Hass):

    def initialize(self):
        self.restore_temp = self.safe_get_float("input_number.away_mode_target_temperature", DEFAULT_AWAY_MODE_TEMP)
        self.heat_to_temp = self.safe_get_float("input_number.away_mode_peak_heat_to_tempearture", DEFAULT_PEAK_HEAT_TEMP)

        # Define custom heating durations for each zone (in seconds)
        self.heat_durations = {
            "climate.main_floor": 40 * 60,
            "climate.master_bedroom": 20 * 60,
            "climate.basement_master": 20 * 60,
            "climate.basement_bunk_rooms": 30 * 60,
            "climate.ski_room": 10 * 60
        }

        self.full_entity_list = list(self.heat_durations.keys())
        self.active_queue = []  # Will store entities to run

        # Optional trigger
        self.listen_state(self.start_override, "input_boolean.start_peak_efficiency", new="on")

        # Run daily at 3:00 PM
        run_at = time(15, 0, 0)  # 3:00 PM
        self.run_daily(self.start_override, run_at)
        
        #using timer helper from home assistant to restore the temperature even if home assistant reboots
        self.listen_event(self.restore_temperature, "timer.finished", entity_id="timer.peak_efficiency_retore_temperature")        

        run_at_am_pm = run_at.strftime("%I:%M %p")
        self.log(f"PeakEfficiency initialized, will run daily at {run_at_am_pm}.")

    def safe_get_float(self, entity_id, default):
        try:
            return float(self.get_state(entity_id))
        except (TypeError, ValueError):
            self.log(f"Could not read {entity_id}, using default {default}", level="WARNING")
            return default

    def start_override(self, entity=None, attribute=None, old=None, new=None, kwargs=None):
        # Create a queue of entities that are in heat mode
        self.active_queue = [e for e in self.full_entity_list if self.get_state(e) == "heat"]

        if not self.active_queue:
            self.log("No climate entities in heat mode — nothing to do.")
            return

        self.log(f"Starting peak override for {len(self.active_queue)} climate entities.")
        self.process_next_climate()

    def process_next_climate(self, kwargs=None):
        if not self.active_queue:
            self.log("All climate entities have been processed.")
            return

        climate = self.active_queue.pop(0)
        heat_duration = self.heat_durations.get(climate, DEFAULT_HEATING_DURATION)  # Default to 20 minutes if not specified
        self.log(f"Overriding {climate} to {self.heat_to_temp}C for {heat_duration // 60} minutes.")

        do_dry_run = self.get_state("input_boolean.peak_efficiency_dry_run") == "on"
        if not do_dry_run:
            self.call_service("climate/set_temperature", entity_id=climate, temperature=self.heat_to_temp)
        else:
            self.log(f"{climate}: Not modifying temperature as Dry Run mode is enabled")

        outside_temp = self.get_state("sensor.condenser_temperature_sensor_temperature")
        current_temp = self.get_state(climate, attribute="current_temperature")

        # Schedule restore after heat_duration
        #self.run_in(self.restore_temperature, heat_duration, climate=climate, outside_temp=outside_temp, start_temp=current_temp)
        
        state = {
            "climate": climate,
            "outside_temp": outside_temp,
            "start_temp": current_temp
        } 
        self.call_service("input_text/set_value", entity_id="input_text.peakefficiency_restore_state", value=json.dumps(state))
        
        #convert duration in sections to "HH:MM:SS" string format
        duration_str = str(timedelta(seconds=heat_duration))
        print(f"Duration string: {duration_str}")
        self.call_service("timer/start", entity_id="timer.peak_efficiency_retore_temperature", duration=duration_str)

    def restore_temperature(self):
        
        raw_state = self.get_state("input_text.heat_restore_state")
        state_info = json.loads(raw_state)
        climate = state_info["climate"]
        outside_temp = state_info["outside_temp"]
        start = state_info["start_temp"]
        current = self.get_state(climate, attribute="current_temperature")
        do_dry_run = self.get_state("input_boolean.peak_efficiency_dry_run") == "on"
        if not do_dry_run: 
            self.call_service("climate/set_temperature", entity_id=climate, temperature=self.restore_temp)
        else:
            self.log(f"{climate}: Not modifying temperature as Dry Run mode is enabled")

        self.log(f'Restored {climate} to {self.restore_temp}C | Outside: {outside_temp}C | Start: {start}C | End: {current}C')

        # Process the next entity after this one finishes
        self.process_next_climate()
        
    def terminate(self):
        #not using this for now
        pass
