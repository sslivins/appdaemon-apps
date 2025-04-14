from datetime import time
import hassapi as hass
from datetime import timedelta
import json
from datetime import datetime, timezone
from dataclasses import dataclass, asdict, fields
import requests
import math

DEFAULT_HEATING_DURATION = 20 * 60  # Default heating duration in seconds
DEFAULT_PEAK_HEAT_TEMP = 19.5  # Default peak heating temperature in Celsius
DEFAULT_AWAY_MODE_TEMP = 13  # Default away mode temperature in Celsius

#home assistant helpers
RESTORE_TEMPERATURE_TIMER = "timer.peak_efficiency_retore_temperature"
MANUAL_START = "input_boolean.start_peak_efficiency"
DRY_RUN = "input_boolean.peak_efficiency_dry_run"
CLIMATE_STATE = "input_text.peakefficiency_restore_state"
OUTDOOR_TEMPERATURE_SENSOR = "sensor.condenser_temperature_sensor_temperature"
AWAY_TARGET_TEMP = "input_number.away_mode_target_temperature"
AWAY_PEAK_HEAT_TO_TEMP = "input_number.away_mode_peak_heat_to_tempearture"


@dataclass
class ClimateState:
    climate: str
    outside_temp: float
    start_temp: float

    def to_json(self):
        """Convert the dataclass to a JSON string."""
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(json_str):
        """Create a ClimateState instance from a JSON string."""
        data = json.loads(json_str)
        return ClimateState(**data)

class PeakEfficiency(hass.Hass):

    def initialize(self):
        #make sure helpers exist, otherwise error out
        #check if the timer exists
        self.assert_entity_exists(RESTORE_TEMPERATURE_TIMER, "Peak Efficiency Restore Timer")
        self.assert_entity_exists(CLIMATE_STATE, "Peak Efficiency Climate State Buffer")
        self.assert_entity_exists(MANUAL_START, "Peak Efficiency Manual Start", required=False)
        self.assert_entity_exists(DRY_RUN, "Peak Efficiency Dry Run", required=False)
        self.assert_entity_exists(OUTDOOR_TEMPERATURE_SENSOR, "Outdoor Temperature Sensor", required=False)
        self.assert_entity_exists(AWAY_TARGET_TEMP, "Away Mode Target Temperature", required=False)
        self.assert_entity_exists(AWAY_PEAK_HEAT_TO_TEMP, "Away Mode Peak Heat Temperature", required=False)
        
        self.restore_temp = self.safe_get_float(AWAY_TARGET_TEMP, DEFAULT_AWAY_MODE_TEMP)
        self.heat_to_temp = self.safe_get_float(AWAY_PEAK_HEAT_TO_TEMP, DEFAULT_PEAK_HEAT_TEMP)

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
        self.listen_state(self.start_override, MANUAL_START, new="on")

        run_at = time(15, 0, 0)  # 3:00 PM
        self.run_daily(self.start_override, run_at)
        
        #check if timer is running which means we are in the middle of a run
        timer_state = self.get_state(RESTORE_TEMPERATURE_TIMER)
        if timer_state == "active":
            #get the state info
            climate_state = self.get_climate_state(clear_after_reading=False)
            #get time left on timer
            hours, minutes, seconds = 0, 0, 0
            finishes_at = self.get_state(RESTORE_TEMPERATURE_TIMER, attribute="finishes_at")
            if finishes_at:
                finishes_at_dt = datetime.fromisoformat(finishes_at)
                now = datetime.now(timezone.utc)
                time_left = finishes_at_dt - now

                if time_left.total_seconds() > 0:
                    hours, remainder = divmod(time_left.total_seconds(), 3600)
                    minutes, seconds = divmod(remainder, 60)
            else:
                self.log("Could not retrieve 'finishes_at' attribute from the timer.")
            
                        
            self.log(f"PeakEfficiency timer is active for {climate_state.climate}, temperature will be restored in {int(hours)} hours, {int(minutes)} minutes, {int(seconds)} seconds.")
        
        #using timer helper from home assistant to restore the temperature even if home assistant reboots
        self.listen_event(self.restore_temperature, "timer.finished", entity_id=RESTORE_TEMPERATURE_TIMER)
        
        lat = self.args.get("latitude")
        lon = self.args.get("longitude")

        if lat is not None and lon is not None:
            forecast = self.get_hourly_forecast(lat, lon, hours=24)
            
            #get total run time of heat_durations
            total_run_time = sum(self.heat_durations.values())
            
            best_start_time, _ =self.warmest_hours(forecast, total_run_time)
            
            self.log(f"Best start time for peak efficiency is {best_start_time}.")
            

        run_at_am_pm = run_at.strftime("%I:%M %p")
        self.log(f"PeakEfficiency initialized, will run daily at {run_at_am_pm}.")

    def safe_get_float(self, entity_id, default):
        try:
            return float(self.get_state(entity_id))
        except (TypeError, ValueError):
            self.log(f"Could not read {entity_id}, using default {default}", level="WARNING")
            return default
        
    def assert_entity_exists(self, entity_id, friendly_name=None, required=True):
        """
        Check if an entity exists in Home Assistant. If it doesn't, log an error and raise a ValueError.
        :param entity_id: The entity ID to check.
        :param friendly_name: Optional friendly name for logging.
        :param required: If True, raise an error if the entity does not exist.
        """
        entity_state = self.get_state(entity_id)
        
        if entity_state is not None:
            return
        
        if required:
            friendly_name = friendly_name or entity_id
            self.error(f"Entity {friendly_name} ({entity_id}) does not exist in Home Assistant.")
            raise ValueError(f"Entity {friendly_name} ({entity_id}) does not exist in Home Assistant.")
        else:
            self.log(f"Entity {friendly_name} ({entity_id}) does not exist in Home Assistant. Proceeding without it.", level="WARNING")
        

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
        heat_duration = self.heat_durations.get(climate, DEFAULT_HEATING_DURATION)
        self.log(f"Overriding {climate} to {self.heat_to_temp}C for {heat_duration // 60} minutes.")

        do_dry_run = self.get_state(DRY_RUN) == "on"
        if not do_dry_run:
            self.call_service("climate/set_temperature", entity_id=climate, temperature=self.heat_to_temp)
            
        self.log(f"{'DRY RUN - ' if do_dry_run else ''}{climate}: Setting temperature to {self.heat_to_temp}C")         

        outside_temp = self.get_state(OUTDOOR_TEMPERATURE_SENSOR)
        current_temp = self.get_state(climate, attribute="current_temperature")

        # Schedule restore after heat_duration
        #self.run_in(self.restore_temperature, heat_duration, climate=climate, outside_temp=outside_temp, start_temp=current_temp)
        
        self.save_climate_state(ClimateState(climate=climate, outside_temp=outside_temp, start_temp=current_temp))
        self.call_service("timer/start", entity_id=RESTORE_TEMPERATURE_TIMER, duration=str(timedelta(seconds=heat_duration)))
        
    def restore_temperature(self, event_name, data, kwargs):
        
        climate_state = self.get_climate_state()

        climate = climate_state.climate
        outside_temp = climate_state.outside_temp
        start_temp = climate_state.start_temp
        current = self.get_state(climate, attribute="current_temperature")
        
        do_dry_run = self.get_state(DRY_RUN) == "on"
        if not do_dry_run: 
            self.call_service("climate/set_temperature", entity_id=climate, temperature=self.restore_temp)

        self.log(f"{'DRY RUN - ' if do_dry_run else ''}{climate}: Restored temperature to {self.restore_temp}C -- Outside: {outside_temp}C | Start: {start_temp}C | End: {current}C")    

        # Process the next entity after this one finishes
        self.process_next_climate()
        
    def save_climate_state(self, state: ClimateState):
        """
        Save a ClimateState object to an input_text entity.
        Raises an error if the input_text entity is not empty.
        """
        current_value = self.get_state(CLIMATE_STATE)
        if current_value != "":
            raise ValueError(f"Cannot save state to {{CLIMATE_STATE}}: it is not empty, got {current_value}")
        
        try:
            self.call_service("input_text/set_value", entity_id=CLIMATE_STATE, value=state.to_json())
            self.log(f"State saved to {{CLIMATE_STATE}}: {state}", level="DEBUG")
        except Exception as e:
            self.error(f"Failed to save state to {{CLIMATE_STATE}}: {e}")
            raise

    def get_climate_state(self, clear_after_reading=True) -> ClimateState:
        """
        Retrieve and decode the state from an input_text entity as a ClimateState object.
        """
        try:
            raw_state = self.get_state(CLIMATE_STATE)
            if not raw_state:
                raise ValueError(f"State in {{CLIMATE_STATE}} is empty or unavailable.")
            if clear_after_reading:
                self.clear_climate_state()
            return ClimateState.from_json(raw_state)
        except json.JSONDecodeError as e:
            self.error(f"Failed to decode state from {{CLIMATE_STATE}}: {e}")
            raise
        except Exception as e:
            self.error(f"Unexpected error while retrieving state from {{CLIMATE_STATE}}: {e}")
            raise

    def clear_climate_state(self):
        """
        Clear the state in an input_text entity by calling save_state with an empty ClimateState.
        """
        try:
            self.call_service("input_text/set_value", entity_id=CLIMATE_STATE, value="")
            self.log(f"State cleared for {{CLIMATE_STATE}}", level="DEBUG")
        except Exception as e:
            self.error(f"Failed to clear state for {{CLIMATE_STATE}}: {e}")
            raise        
        
    def get_hourly_forecast(self, lat, lon, hours=6):
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m,relative_humidity_2m,shortwave_radiation",
            "forecast_days": 1,
            "timezone": "auto"
        }

        response = requests.get(url, params=params)
        data = response.json()

        times = data["hourly"]["time"]
        temps = data["hourly"]["temperature_2m"]
        humidity = data["hourly"]["relative_humidity_2m"]
        radiation = data["hourly"]["shortwave_radiation"]

        # Return a list of tuples for unpacking
        return list(zip(times, temps, humidity, radiation))[:hours]
    
    def warmest_hours(forecast, minutes):
        block_size = math.ceil(minutes / 60)  # round up to full hours
        if len(forecast) < block_size:
            raise ValueError("Forecast data too short for the requested window")

        max_sum = float('-inf')
        best_start_time = None

        # forecast is list of tuples: (time, temp)
        for i in range(len(forecast) - block_size + 1):
            window = forecast[i:i + block_size]
            temp_sum = sum(temp for _, temp in window)

            if temp_sum > max_sum:
                max_sum = temp_sum
                best_start_time = window[0][0]  # timestamp of the first hour

        return best_start_time, block_size
    
        
    def terminate(self):
        #not using this for now
        pass
