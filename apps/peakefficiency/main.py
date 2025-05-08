from datetime import time
import hassapi as hass
from datetime import timedelta, datetime
from dataclasses import dataclass, asdict, fields
from forecast import ForecastSummary, ForecastDailySummary
from utils import HelperUtils
import os
from typing import List, Dict, Optional
from persistent_scheduler import PersistentScheduler
from summary import DailySummary, ZoneEvent

DAILY_SCHEDULE_SOAK_RUN = time(8, 0, 0)  # figure out what time to run the soak run
DEFAULT_RUN_AT_TIME = time(15, 0, 0)  # Default run time is 3 PM
DEFAULT_ZONE_RUN_DURATION = 20 * 60  # Default run duration is 1 hour (3600 seconds)
DEFAULT_PEAK_HEAT_TEMP = 19.5  # Default peak heating temperature in Celsius
DEFAULT_AWAY_MODE_TEMP = 13  # Default away mode temperature in Celsius

#home assistant helpers
MANUAL_START = "input_boolean.start_peak_efficiency"
DRY_RUN = "input_boolean.peak_efficiency_dry_run"
QUICK_RUN = "input_boolean.peak_efficiency_quick_run"
OUTDOOR_TEMPERATURE_SENSOR = "sensor.condenser_temperature_sensor_temperature"
AWAY_TARGET_TEMP = "input_number.away_mode_target_temperature"
AWAY_PEAK_HEAT_TO_TEMP = "input_number.away_mode_peak_heat_to_tempearture"
AWAY_MODE_ENABLED = "input_boolean.home_away_mode_enabled"
PEAK_EFFICIENCY_DISABLED = "input_boolean.peak_efficiency_disabled"

CACHE_PATH = os.path.join(os.path.dirname(__file__), "cache")
ZONE_STATE_KEY = "zone_state"  # Key for storing zone state in the cache

#if you want to create an override for a zone, create an input_number with the name "input_number.peak_efficiency_<entity name without 'climate.'>_run_override"
# e.g. input_number.peak_efficiency_main_floor_run_override

class PeakEfficiency(hass.Hass):

    latitude: Optional[float] = None
    longitude: Optional[float] = None
    schedule_handle: Optional[str] = None
    summary: Optional[DailySummary] = None
    restore_temp: Optional[float] = None
    heat_to_temp: Optional[float] = None
    active_queue: List[str] = []  # Will store entities to run
    climate_entities: List[str] = []  # Will store all entities to run
    all_zones_processed: bool = False  # Flag to check if all zones have been processed
    job_scheduler: Optional[PersistentScheduler] = None
    hvac_action_callback_handles: List[str] = []  # List to store handles for HVAC action callbacks

    def initialize(self):
        
        self.latitude = self.args.get("latitude")
        self.longitude = self.args.get("longitude")

        # Retrieve the list of climate entities from the YAML configuration
        self.climate_entities = self.args.get("climate_entities", [])
        if not self.climate_entities:
            self.log("No climate entities provided in the YAML configuration.", level="ERROR")
            return

        if self.latitude is None or self.longitude is None:
            self.log("Latitude and longitude arguments not provided, forecast will not be used", level="WARNING")
            
        hu = HelperUtils(self)

        # Load the summary from the cache if it exists, otherwise create a new one
        self.summary = DailySummary.load(cache_path=CACHE_PATH, cache_key="daily:summary")
        self.job_scheduler = PersistentScheduler(self, cache_path=CACHE_PATH)
        
        #make sure helpers exist, otherwise error out
        #check if the timer exists
        hu.assert_entity_exists(AWAY_MODE_ENABLED, "Away Mode Enabled")
        
        hu.assert_entity_exists(MANUAL_START, "Peak Efficiency Manual Start", required=False)
        hu.assert_entity_exists(DRY_RUN, "Peak Efficiency Dry Run", required=False)
        hu.assert_entity_exists(OUTDOOR_TEMPERATURE_SENSOR, "Outdoor Temperature Sensor", required=False)
        hu.assert_entity_exists(AWAY_TARGET_TEMP, "Away Mode Target Temperature", required=False)
        hu.assert_entity_exists(AWAY_PEAK_HEAT_TO_TEMP, "Away Mode Peak Heat Temperature", required=False)
        hu.assert_entity_exists(QUICK_RUN, "Peak Efficiency Quick Run", required=False)
        
        self.restore_temp = hu.safe_get_float(AWAY_TARGET_TEMP, DEFAULT_AWAY_MODE_TEMP)
        self.heat_to_temp = hu.safe_get_float(AWAY_PEAK_HEAT_TO_TEMP, DEFAULT_PEAK_HEAT_TEMP)
        
        # Optional trigger
        self.listen_state(self.start_heat_soak, MANUAL_START, new="on")

        # Restore state if the system rebooted
        if self.summary.cache_exists():
            #start with all zones and then remove those that have been completed
            self._create_entity_queue(hvac_mode="heat")

            self.log("Restoring state from cache...", level="INFO")
            for climate_entity in self.summary.get_started_zones():
                self.log(f"Zone {climate_entity} was started already, removing from active queue.", level="DEBUG")
                self.active_queue.remove(climate_entity)
                
            for climate_entity in self.summary.get_completed_zones():
                self.log(f"Zone {climate_entity} was completed, listening for hvac events", level="DEBUG")
                self.hvac_action_callback_handles.append(self.listen_state(self.zone_hvac_action, climate_entity, attribute="hvac_action", old="idle", new="heating"))
                self.hvac_action_callback_handles.append(self.listen_state(self.zone_hvac_action, climate_entity, attribute="hvac_action", old="heating", new="idle"))

            self.log(f"PeakEfficiency Summary: {self.summary}", level="INFO")

        self.schedule_energy_soak_run()

        self.run_daily(self.schedule_energy_soak_run, DAILY_SCHEDULE_SOAK_RUN)
        
        self.log(f"PeakEfficiency initialized.")
        
    def schedule_energy_soak_run(self, entity=None, attribute=None, old=None, new=None, kwargs=None):
        '''Figure out when the best time to run is based on the forecast.'''

        run_at = DEFAULT_RUN_AT_TIME
        
        if self.latitude is not None and self.longitude is not None:
           
            forecastObj = ForecastSummary(self, self.latitude, self.longitude)
            
            forecast = forecastObj.get_forecast_data()
            
            for f_time, f_temp, f_humidity, f_radiation in forecast:
                self.log(f"Forecast for {f_time}: Temp: {f_temp}C, Humidity: {f_humidity}%, Radiation: {f_radiation}W/m2", level="DEBUG")

            total_run_time = self._get_total_zones_duration()    
            
            best_start_time, _ = forecastObj.warmest_hours(total_run_time / 60)
            
            self.log(f"Best start time based on weather forecast is: {best_start_time}", level="INFO")
            
            run_at = best_start_time.time() if best_start_time else run_at
            
        else:
            self.log("Latitude and longitude not set, using default run time.", level="WARNING")
       
        if self.schedule_handle is not None:
            self.log(f"PeakEfficiency already scheduled for {self.schedule_handle}, cancelling it.")
            self.cancel_timer(self.schedule_handle)
            
        #only run this while in away mode
        if self._is_away_mode_enabled():
            self.all_zones_processed = False
            self.schedule_handle = self.run_daily(self.start_heat_soak, run_at)
      
            run_at_am_pm = run_at.strftime("%I:%M %p")
            self.log(f"PeakEfficiency will run today at {run_at_am_pm}.", level="INFO")
        else:
            if not self._is_away_mode_enabled():
                self.log("PeakEfficiency will not run today because away mode is not enabled.", level="INFO")

    def start_heat_soak(self, entity=None, attribute=None, old=None, new=None, kwargs=None):

        if self.summary.date:
            self.finalize_day()
            
        self.summary.set_start_time()
        self.summary.set_forecast(self.latitude, self.longitude)

        if self._is_peak_efficiency_disabled():
            self.log("Peak Efficiency is disabled, not starting heat soak.", level="INFO")
            return

        # Create a queue of entities that are in heat mode
        self._create_entity_queue(hvac_mode="heat")

        if not self.active_queue:
            self.log("No climate entities in heat mode — nothing to do.")
            return

        self.log(f"Starting peak override for {len(self.active_queue)} climate entities.")
        self.process_next_zone()

    def process_next_zone(self, kwargs=None):
        if not self.active_queue:
            self.log("All climate entities have been processed.")
            self.all_zones_processed = True
            return

        climate_entity = self.active_queue.pop(0)
        run_duration = self._get_zone_run_duration(climate_entity)
        self.log(f"Overriding {climate_entity} to {self.heat_to_temp}C for {run_duration // 60} minutes.")

        temp_modified = False

        do_dry_run = self._is_dry_run()
        if not do_dry_run and run_duration > 0:
            self.call_service("climate/set_temperature", entity_id=climate_entity, temperature=self.heat_to_temp)
            temp_modified = True
            
        self.log(f"{'DRY RUN - ' if do_dry_run else ''}{climate_entity}: Setting temperature to {self.heat_to_temp}C")         

        outside_temp = self.get_state(OUTDOOR_TEMPERATURE_SENSOR)
        current_temp = self.get_state(climate_entity, attribute="current_temperature")

        end_time = datetime.now() + timedelta(seconds=run_duration)

        self.summary.start_zone(
            climate_entity=climate_entity,
            hvac_action="heating",
            start_time=datetime.now(),
            end_time=end_time,
            target_temp=self.restore_temp,
            start_temp=current_temp,
            outside_temp=outside_temp
        )

        job_id = self.job_scheduler.schedule(self.complete_zone, end_time, kwargs={"climate_entity": climate_entity, "run_duration": run_duration, "temp_modified": temp_modified})
        self.log(f"Scheduled job '{job_id}' to run in {run_duration} seconds for {climate_entity}.")
        
    def complete_zone(self, kwargs=None):
        
        climate_entity = kwargs.get("climate_entity")
        if climate_entity is None:
            self.log("No climate entity provided, cannot complete zone.")
            return
        
        #indicates that the zone temperature was modified, default to True as a failsafe in case it's not present
        temp_modified = kwargs.get("temp_modified", True)

        current = self.get_state(climate_entity, attribute="current_temperature")
        self.log(f"Completing zone {climate_entity} with current temperature: {current}C, restore temperature to: {self.restore_temp}C")
        
        do_dry_run = self._is_dry_run()
        if not do_dry_run and temp_modified:
            self.call_service("climate/set_temperature", entity_id=climate_entity, temperature=self.restore_temp)

        self.log(f"{'DRY RUN - ' if do_dry_run else ''}{climate_entity}: Restored temperature to {self.restore_temp}C")  
        
        self.summary.complete_zone(climate_entity=climate_entity, end_temp=current)
        
        delay_check_temp_1 = 30 if self._is_quick_run() else 30 * 60
        delay_check_temp_2 = 60 if self._is_quick_run() else 60 * 60
        
        self.job_scheduler.schedule(self.delayed_get_temperature, datetime.now() + timedelta(seconds=delay_check_temp_1), kwargs={"climate_entity": climate_entity})
        self.job_scheduler.schedule(self.delayed_get_temperature, datetime.now() + timedelta(seconds=delay_check_temp_2), kwargs={"climate_entity": climate_entity})
            

        self._wait_for_state(climate_entity, attribute="hvac_action", expected_state="idle", timeout=10)        
        
        #get notified if this zone ever starts heating again        
        self.hvac_action_callback_handles.append(self.listen_state(self.zone_hvac_action, climate_entity, attribute="hvac_action", old="idle", new="heating"))
        self.hvac_action_callback_handles.append(self.listen_state(self.zone_hvac_action, climate_entity, attribute="hvac_action", old="heating", new="idle"))

        # Process the next entity after this one finishes
        self.process_next_zone()
        
    def zone_hvac_action(self, entity=None, attribute=None, old=None, new=None, kwargs=None):
        """
        Callback function to handle the HVAC action of a zone.
        """
        self.log(f"Zone {entity} HVAC action changed from {old} to {new}.")
        if old == "heating" and new == "idle":
            self.log(f"Zone {entity} finished heating.")
            self.summary.complete_unplanned_hvac_action(entity, datetime.now())
        elif old == "idle" and new == "heating":
            self.log(f"Zone {entity} is heating.")
            self.summary.start_unplanned_hvac_action(entity, new, datetime.now())            

    def delayed_get_temperature(self, kwargs=None):

        climate_entity = kwargs.get("climate_entity")
        if climate_entity is None:
            self.log("No climate entity provided, cannot get delayed temperature.")
            return

        current_temp = self.get_state(climate_entity, attribute="current_temperature")

        record = self.summary.add_delay_temperature(climate_entity=climate_entity, temperature=current_temp, timestamp=datetime.now())
        self.log(f"{climate_entity}: Delayed temperature: Got Current Temperature after {record.seconds_after_end} seconds: {current_temp}C", level="DEBUG")

    def _create_entity_queue(self, hvac_mode: str = "heat"):
        """
        Create a queue of entities that are in the specified HVAC mode.
        """
        self.active_queue = [e for e in self.climate_entities if self.get_state(e) == hvac_mode]

    def finalize_day(self):
        
        #cancel and remove all hvac action callbacks
        for handle in self.hvac_action_callback_handles:
            self.cancel_listen_state(handle)
        self.hvac_action_callback_handles.clear()

        #get current temperature for all zones
        for entity in self.climate_entities:
            current_temp = self.get_state(entity, attribute="current_temperature")
            self.summary.add_delay_temperature(climate_entity=entity, temperature=current_temp, timestamp=datetime.now())

        self.summary.write_summary_to_csv("event_log.csv")

        """
        Finalize the day by saving the summary and clearing the cache.
        """
        self.log_summary()

        ## Clear the cache for the next day
        self.summary.clear()

        # clear the summary memory structure by reloading the cleared cache
        self.summary = DailySummary.load(cache_path=CACHE_PATH, cache_key="daily:summary")

        self.log("Finalized day, summary saved.", level="INFO")

    def log_summary(self):
        #for now just print it
        self.log(f"PeakEfficiency Summary: {self.summary}", level="INFO")
                    
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
    
    def _is_dry_run(self):
        """
        Check if the dry run mode is enabled.
        """
        return self.get_state(DRY_RUN) == "on"
    
    def _is_quick_run(self):
        """
        Check if the quick run mode is enabled.
        """
        return self.get_state(QUICK_RUN) == "on"
    
    def _wait_for_state(self, entity, attribute, expected_state, timeout=60):
        """
        Waits for a specific state change on a given entity within a specified timeout period.

        This method continuously checks the state of the specified entity's attribute until it matches
        the expected state or the timeout period elapses. If the state matches the expected state
        within the timeout, the method returns `True`. Otherwise, it logs a warning and returns `False`.

        Args:
            entity (str): The name of the entity to monitor.
            attribute (str): The specific attribute of the entity to check.
            expected_state (Any): The state value to wait for.
            timeout (int, optional): The maximum time to wait for the state change, in seconds. 
                                     Defaults to 60 seconds.

        Returns:
            bool: `True` if the entity's attribute matches the expected state within the timeout period,
                  `False` otherwise.

        Logs:
            Logs a warning message if the timeout period elapses without the entity's attribute
            reaching the expected state.
        
        """
        start_time = datetime.now()
        while True:
            current_state = self.get_state(entity, attribute=attribute)
            if current_state == expected_state:
                return True
            if (datetime.now() - start_time).total_seconds() > timeout:
                self.log(f"Timeout waiting for {entity} to change to {expected_state} got {current_state}.", level="WARNING")
                return False
            time.sleep(1)

    def _get_zone_run_duration(self, entity: str) -> Optional[float]:
        """
        Get the run duration for a specific zone.

        This method checks if there is an override value for the specified zone. If an override exists,
        it will use that value (converted to seconds). Otherwise, it falls back to the default duration
        defined in `self.heat_durations`.

        Args:
            entity (str): The name of the entity to check.

        Returns:
            Optional[float]: The run duration in seconds, or None if not found.
        """
        if self._is_quick_run():
            return 1 * 60
        
        # Guess the override entity name based on the climate entity name
        entity_suffix = entity.split(".")[-1]  # Extract the suffix (e.g., "main_floor")
        override_entity = f"input_number.peak_efficiency_{entity_suffix}_run_override"
        if override_entity:
            override_value = self.get_state(override_entity)
            if override_value is not None and float(override_value) > 0:
                return float(override_value) * 60  # Convert minutes to seconds
            
        return DEFAULT_ZONE_RUN_DURATION
    
    def _get_total_zones_duration(self) -> int:
        """
        Get the total run durations for all zones.

        This method loops through all climate entities, retrieves their run durations using
        the `_get_zone_run_duration` function, and returns the total sum.

        Returns:
            int: The total run duration for all zones in seconds.
        """
        total_duration = 0
        for entity in self.climate_entities:
            duration = self._get_zone_run_duration(entity)
            if duration:
                total_duration += duration
        return total_duration
            
    def terminate(self):
        #not using this for now
        pass

