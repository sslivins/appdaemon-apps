from datetime import time
import hassapi as hass
from datetime import timedelta
import json
from datetime import datetime, timezone
from dataclasses import dataclass, asdict, fields
from forecast import ForecastSummary
from utils import HelperUtils
from diskcache import Cache
import os


DAILY_SCHEDULE_SOAK_RUN = time(8, 0, 0)  # figure out what time to run the soak run
DEFAULT_RUN_AT_TIME = time(15, 0, 0)  # Default run time is 3 PM
DEFAULT_HEATING_DURATION = 20 * 60  # Default heating duration in seconds
DEFAULT_PEAK_HEAT_TEMP = 19.5  # Default peak heating temperature in Celsius
DEFAULT_AWAY_MODE_TEMP = 13  # Default away mode temperature in Celsius

#home assistant helpers
RESTORE_TEMPERATURE_TIMER = "timer.peak_efficiency_retore_temperature"
MANUAL_START = "input_boolean.start_peak_efficiency"
DRY_RUN = "input_boolean.peak_efficiency_dry_run"
CLIMATE_STATE = "input_text.peakefficiency_restore_state"
ZONE_STATE_KEY = "zone_state"  # Key for storing zone state in the cache
OUTDOOR_TEMPERATURE_SENSOR = "sensor.condenser_temperature_sensor_temperature"
AWAY_TARGET_TEMP = "input_number.away_mode_target_temperature"
AWAY_PEAK_HEAT_TO_TEMP = "input_number.away_mode_peak_heat_to_tempearture"
AWAY_MODE_ENABLED = "input_boolean.home_away_mode_enabled"
PEAK_EFFICIENCY_DISABLED = "input_boolean.peak_efficiency_disabled"


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
        
        self.latitude = self.args.get("latitude")
        self.longitude = self.args.get("longitude")
        
        if self.latitude is None or self.longitude is None:
            self.log("Latitude and longitude arguments not provided, forecast will not be used", level="WARNING")
            
        hu = HelperUtils(self)
        
        self.schedule_handle = None
        #make sure helpers exist, otherwise error out
        #check if the timer exists
        hu.assert_entity_exists(RESTORE_TEMPERATURE_TIMER, "Peak Efficiency Restore Timer")
        #hu.assert_entity_exists(CLIMATE_STATE, "Peak Efficiency Climate State Buffer")
        hu.assert_entity_exists(AWAY_MODE_ENABLED, "Away Mode Enabled")
        
        hu.assert_entity_exists(MANUAL_START, "Peak Efficiency Manual Start", required=False)
        hu.assert_entity_exists(DRY_RUN, "Peak Efficiency Dry Run", required=False)
        hu.assert_entity_exists(OUTDOOR_TEMPERATURE_SENSOR, "Outdoor Temperature Sensor", required=False)
        hu.assert_entity_exists(AWAY_TARGET_TEMP, "Away Mode Target Temperature", required=False)
        hu.assert_entity_exists(AWAY_PEAK_HEAT_TO_TEMP, "Away Mode Peak Heat Temperature", required=False)
        
        cache_dir = os.path.join(os.path.dirname(__file__), "cache")
        self.log(f"Cache directory: {cache_dir}", level="DEBUG")
        self.cache = Cache(cache_dir)
        
        self.restore_temp = hu.safe_get_float(AWAY_TARGET_TEMP, DEFAULT_AWAY_MODE_TEMP)
        self.heat_to_temp = hu.safe_get_float(AWAY_PEAK_HEAT_TO_TEMP, DEFAULT_PEAK_HEAT_TEMP)

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
        self.listen_state(self.start_heat_soak, MANUAL_START, new="on")

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
                    
                #add the remaining thermostats to the queue after the current one
                if climate_state and climate_state.climate in self.full_entity_list:
                    start_index = self.full_entity_list.index(climate_state.climate) + 1
                    self.active_queue = []
                    found_current = False
                    for entity in self.full_entity_list:
                        if entity == climate_state.climate:
                            found_current = True
                            continue
                        if found_current and self.get_state(entity) == "heat":
                            self.active_queue.append(entity)
            else:
                self.log("Could not retrieve 'finishes_at' attribute from the timer.")
            
            self.log(f"PeakEfficiency timer is active for {climate_state.climate}, temperature will be restored in {int(hours)} hours, {int(minutes)} minutes, {int(seconds)} seconds.")
        
        #using timer helper from home assistant to restore the temperature even if home assistant reboots
        self.listen_event(self.stop_heat_soak, "timer.finished", entity_id=RESTORE_TEMPERATURE_TIMER)
        
        #run manually and then run the scheduler daily to figure when the best time to run override based on the weather forecast
        self.schedule_energy_soak_run()
        self.run_daily(self.schedule_energy_soak_run, DAILY_SCHEDULE_SOAK_RUN)
        
        self.log(f"PeakEfficiency initialized.")
        
    def schedule_energy_soak_run(self, entity=None, attribute=None, old=None, new=None, kwargs=None):
        '''Figure out when the best time to run is based on the forecast.'''
        
        run_at = DEFAULT_RUN_AT_TIME
        
        if self.latitude is not None and self.longitude is not None:
            forecastSummary = ForecastSummary(self, self.latitude, self.longitude)
            
            forecast = forecastSummary.get_forecast_data()
            
            for f_time, f_temp, f_humidity, f_radiation in forecast:
                self.log(f"Forecast for {f_time}: Temp: {f_temp}C, Humidity: {f_humidity}%, Radiation: {f_radiation}W/m2", level="DEBUG")
            
            #get total run time of heat_durations
            total_run_time = sum(self.heat_durations.values()) / 60  # convert to minutes
            
            best_start_time, _ = forecastSummary.warmest_hours(total_run_time)
            
            self.log(f"Best start time based on weather forecast is: {best_start_time}", level="INFO")
            
            run_at = best_start_time.time() if best_start_time else run_at
        else:
            self.log("Latitude and longitude not set, using default run time.", level="WARNING")
       
        if self.schedule_handle is not None:
            self.log(f"PeakEfficiency already scheduled for {self.schedule_handle}, cancelling it.")
            self.cancel_timer(self.schedule_handle)
            
        #only run this while in away mode
        if self._is_away_mode_enabled():
            self.schedule_handle = self.run_daily(self.start_heat_soak, run_at)
      
            run_at_am_pm = run_at.strftime("%I:%M %p")
            self.log(f"PeakEfficiency will run today at {run_at_am_pm}.", level="INFO")
        else:
            if not self._is_away_mode_enabled():
                self.log("PeakEfficiency will not run today because away mode is not enabled.", level="INFO")


    def start_heat_soak(self, entity=None, attribute=None, old=None, new=None, kwargs=None):

        if self._is_peak_efficiency_disabled():
            self.log("Peak Efficiency is disabled, not starting heat soak.", level="INFO")
            return

        # Create a queue of entities that are in heat mode
        self.active_queue = [e for e in self.full_entity_list if self.get_state(e) == "heat"]

        if not self.active_queue:
            self.log("No climate entities in heat mode — nothing to do.")
            return

        self.log(f"Starting peak override for {len(self.active_queue)} climate entities.")
        self.process_next_zone()

    def process_next_zone(self, kwargs=None):
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

        self.save_climate_state(ClimateState(climate=climate, outside_temp=outside_temp, start_temp=current_temp))
        self.call_service("timer/start", entity_id=RESTORE_TEMPERATURE_TIMER, duration=str(timedelta(seconds=heat_duration)))
        
    def stop_heat_soak(self, event_name, data, kwargs):
        
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
        self.process_next_zone()
        
    def save_climate_state(self, state: ClimateState):
        """
        Save a ClimateState object to an input_text entity.
        Raises an error if the input_text entity is not empty.
        """
        #current_value = self.get_state(CLIMATE_STATE)
        climate_state = self.cache.get(ZONE_STATE_KEY, default=None)
        if climate_state is not None:
            raise ValueError(f"Cannot save zone state: it is not empty, got {climate_state}")
        
        try:
            #self.call_service("input_text/set_value", entity_id=CLIMATE_STATE, value=state.to_json())
            self.cache.set(ZONE_STATE_KEY, state.to_json())
            self.log(f"State saved: {state}", level="DEBUG")
        except Exception as e:
            self.error(f"Failed to save zone state: {e}")
            raise

    def get_climate_state(self, clear_after_reading=True) -> ClimateState:
        """
        Retrieve and decode the state from an input_text entity as a ClimateState object.
        """
        try:
            raw_state = self.cache.get(ZONE_STATE_KEY, default=None)
            if not raw_state:
                raise ValueError(f"zone state is empty or unavailable.")
            if clear_after_reading:
                self.cache.delete(ZONE_STATE_KEY)
            return ClimateState.from_json(raw_state)
        except json.JSONDecodeError as e:
            self.error(f"Failed to decode zone state: {e}")
            raise
        except Exception as e:
            self.error(f"Unexpected error while retrieving zone state: {e}")
            raise

    def _is_away_mode_enabled(self):
        """
        Check if the home/away mode is enabled.
        """
        return self.get_state(AWAY_MODE_ENABLED) == "on"
    
    def _is_peak_efficiency_disabled(self):
        """
        Check if the peak efficiency is disabled.
        """
        return self.get_state(PEAK_EFFICIENCY_DISABLED) == "on"
        
    def terminate(self):
        #not using this for now
        pass

