from datetime import time
import hassapi as hass
from datetime import timedelta
import json
from datetime import datetime, timezone
from dataclasses import dataclass, asdict, fields
from forecast import ForecastSummary, ForecastDailySummary
from utils import HelperUtils
from pydantic import BaseModel, PrivateAttr, ConfigDict
from diskcache import Cache
from typing import Type, TypeVar
import os
from typing import List, Dict, Optional
from persistent_scheduler import PersistentScheduler


DAILY_SCHEDULE_SOAK_RUN = time(8, 0, 0)  # figure out what time to run the soak run
DEFAULT_RUN_AT_TIME = time(15, 0, 0)  # Default run time is 3 PM
DEFAULT_HEATING_DURATION = 20 * 60  # Default heating duration in seconds
DEFAULT_PEAK_HEAT_TEMP = 19.5  # Default peak heating temperature in Celsius
DEFAULT_AWAY_MODE_TEMP = 13  # Default away mode temperature in Celsius

#home assistant helpers
MANUAL_START = "input_boolean.start_peak_efficiency"
DRY_RUN = "input_boolean.peak_efficiency_dry_run"
OUTDOOR_TEMPERATURE_SENSOR = "sensor.condenser_temperature_sensor_temperature"
AWAY_TARGET_TEMP = "input_number.away_mode_target_temperature"
AWAY_PEAK_HEAT_TO_TEMP = "input_number.away_mode_peak_heat_to_tempearture"
AWAY_MODE_ENABLED = "input_boolean.home_away_mode_enabled"
PEAK_EFFICIENCY_DISABLED = "input_boolean.peak_efficiency_disabled"


CACHE_PATH = os.path.join(os.path.dirname(__file__), "cache")
ZONE_STATE_KEY = "zone_state"  # Key for storing zone state in the cache

    
T = TypeVar("T", bound="PersistentBase")



class PersistentBase(BaseModel):
    _cache: Cache = PrivateAttr(default_factory=lambda: Cache(CACHE_PATH))
    _cache_key: str = ""

    model_config = ConfigDict(arbitrary_types_allowed=True) 

    def save(self):
        data = self.model_dump()
        self._cache.set(self._cache_key, data)

    @classmethod
    def load(cls, cache_key: str):
        instance = cls()
        data = instance._cache.get(cache_key)
        if data:
            instance = cls(**data)
        instance._cache_key = cache_key
        return instance

    def clear(self):
        self._cache.delete(self._cache_key)

   
class TemperatureRecord(BaseModel):
    temperature: float
    timestamp: datetime
    minutes_after_end: float


class ZoneSummary(BaseModel):
    zone: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    duration: Optional[int] = None
    start_temp: Optional[float] = None
    outside_temp: Optional[float] = None  # taken at start
    end_temp: Optional[float] = None
    completed: bool = False  # Flag to indicate if the zone has been completed
    temperature_records: List[TemperatureRecord] = []  # List of temperature records

    def add_end_temperature(self, temperature: float, timestamp: datetime = None):
        """
        Add a temperature record to the list, including the time difference from end_time.
        """
        if self.end_time is None:
            raise ValueError("end_time must be set before adding temperature records.")
        
        timestamp = timestamp or datetime.now()

        time_difference = (timestamp - self.end_time).total_seconds() / 60  # Calculate time difference in minutes
        record = TemperatureRecord(
            temperature=temperature,
            timestamp=timestamp,
            minutes_after_end=time_difference
        )
        self.temperature_records.append(record)

        return record


class DailySummary(PersistentBase):
    date: Optional[datetime] = None
    forecast: Optional[ForecastDailySummary] = None
    zones: Optional[Dict[str, ZoneSummary]] = {}

    def __init__(self, cache_key: str = "", **data):
        super().__init__(**data)
        self._cache_key = cache_key

    def __str__(self):
        """Provide a string representation of the DailySummary for printing."""
        return json.dumps(
            {
                "date": self.date.isoformat() if self.date else None,
                "forecast": self.forecast.model_dump() if self.forecast else None,
                "zones": {k: v.model_dump() for k, v in self.zones.items()} if self.zones else {},
            },
            indent=2,
            default=str,
        )
    
class PeakEfficiency(hass.Hass):

    latitude: Optional[float] = None
    longitude: Optional[float] = None
    schedule_handle: Optional[str] = None
    cache: Optional[Cache] = None
    summary: Optional[DailySummary] = None
    restore_temp: Optional[float] = None
    heat_to_temp: Optional[float] = None
    active_queue: List[str] = []  # Will store entities to run
    full_entity_list: List[str] = []  # Will store all entities to run
    heat_durations: Dict[str, int] = {}  # Will store custom heating durations for each zone
    all_zones_processed: bool = False  # Flag to check if all zones have been processed
    job_scheduler: Optional[PersistentScheduler] = None

    def initialize(self):
        
        self.latitude = self.args.get("latitude")
        self.longitude = self.args.get("longitude")
        
        if self.latitude is None or self.longitude is None:
            self.log("Latitude and longitude arguments not provided, forecast will not be used", level="WARNING")
            
        hu = HelperUtils(self)

        # Load the summary from the cache if it exists, otherwise create a new one
        self.summary = DailySummary.load("daily:summary")
        self.job_scheduler = PersistentScheduler(self, cache_path=CACHE_PATH)
        
        #make sure helpers exist, otherwise error out
        #check if the timer exists
        hu.assert_entity_exists(AWAY_MODE_ENABLED, "Away Mode Enabled")
        
        hu.assert_entity_exists(MANUAL_START, "Peak Efficiency Manual Start", required=False)
        hu.assert_entity_exists(DRY_RUN, "Peak Efficiency Dry Run", required=False)
        hu.assert_entity_exists(OUTDOOR_TEMPERATURE_SENSOR, "Outdoor Temperature Sensor", required=False)
        hu.assert_entity_exists(AWAY_TARGET_TEMP, "Away Mode Target Temperature", required=False)
        hu.assert_entity_exists(AWAY_PEAK_HEAT_TO_TEMP, "Away Mode Peak Heat Temperature", required=False)
        
        self.restore_temp = hu.safe_get_float(AWAY_TARGET_TEMP, DEFAULT_AWAY_MODE_TEMP)
        self.heat_to_temp = hu.safe_get_float(AWAY_PEAK_HEAT_TO_TEMP, DEFAULT_PEAK_HEAT_TEMP)

        if self._is_dry_run():
            self.heat_durations = {
                "climate.main_floor": 1 * 60,
                "climate.master_bedroom": 1 * 60,
                "climate.basement_master": 1 * 60,
                "climate.basement_bunk_rooms": 1 * 60,
                "climate.ski_room": 1 * 60
            }
        else:              
        
            #Define custom heating durations for each zone (in seconds)
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

        # Restore state if the system rebooted
        if self.summary.zones:
            #start with all zones and then remove those that have been completed
            self._create_entity_queue(hvac_mode="heat")

            self.log("Restoring state from cache...", level="INFO")
            for climate_entity, zone_summary in self.summary.zones.items():
                self.log(f"Zone {climate_entity} was started already, removing from active queue.", level="DEBUG")
                self.active_queue.remove(climate_entity)

            self.log(f"PeakEfficiency Summary: {self.summary}", level="INFO")

        else:
            #run manually and then run the scheduler daily to figure when the best time to run override based on the weather forecast
            self.schedule_energy_soak_run()


        self.run_daily(self.schedule_energy_soak_run, DAILY_SCHEDULE_SOAK_RUN)
        
        self.log(f"PeakEfficiency initialized.")
        
    def schedule_energy_soak_run(self, entity=None, attribute=None, old=None, new=None, kwargs=None):
        '''Figure out when the best time to run is based on the forecast.'''

        run_at = DEFAULT_RUN_AT_TIME
        
        if self.latitude is not None and self.longitude is not None:
           
            #get total run time of heat_durations
            total_run_time = sum(self.heat_durations.values()) / 60  # convert to minutes
            
            forecastObj = ForecastSummary(self, self.latitude, self.longitude)
            
            forecase = forecastObj.get_forecast_data()
            
            for f_time, f_temp, f_humidity, f_radiation in forecase:
                self.log(f"Forecast for {f_time}: Temp: {f_temp}C, Humidity: {f_humidity}%, Radiation: {f_radiation}W/m2", level="DEBUG")
            
            best_start_time, _ = forecastObj.warmest_hours(total_run_time)
            
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
            
        self.summary.date = datetime.now()
        
        forecast = ForecastSummary(self, self.latitude, self.longitude)     
        self.summary.forecast = forecast.summarize()
        self.summary.save()

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
        heat_duration = self.heat_durations.get(climate_entity, DEFAULT_HEATING_DURATION)
        self.log(f"Overriding {climate_entity} to {self.heat_to_temp}C for {heat_duration // 60} minutes.")

        do_dry_run = self._is_dry_run()
        if not do_dry_run:
            self.call_service("climate/set_temperature", entity_id=climate_entity, temperature=self.heat_to_temp)
            
        self.log(f"{'DRY RUN - ' if do_dry_run else ''}{climate_entity}: Setting temperature to {self.heat_to_temp}C")         

        outside_temp = self.get_state(OUTDOOR_TEMPERATURE_SENSOR)
        current_temp = self.get_state(climate_entity, attribute="current_temperature")

        zone_summary = ZoneSummary(
            zone=climate_entity,
            start_time=datetime.now(),
            end_time=datetime.now() + timedelta(seconds=heat_duration),
            duration=heat_duration,
            start_temp=current_temp,
            outside_temp=outside_temp
        )
        
        self.summary.zones[climate_entity] = zone_summary
        self.summary.save()

        job_id = self.job_scheduler.schedule(self.complete_zone, zone_summary.end_time, kwargs={"climate_entity": climate_entity})
        self.log(f"Scheduled job '{job_id}' to run in {heat_duration} seconds for {climate_entity}.")
        
    def complete_zone(self, kwargs=None):
        
        climate_entity = kwargs.get("climate_entity")
        if climate_entity is None:
            self.log("No climate entity provided, cannot complete zone.")
            return

        current = self.get_state(climate_entity, attribute="current_temperature")
        self.log(f"Completing zone {climate_entity} with current temperature: {current}C, restore temperature to: {self.restore_temp}C")
        
        do_dry_run = self._is_dry_run()
        if not do_dry_run: 
            self.call_service("climate/set_temperature", entity_id=climate_entity, temperature=self.restore_temp)

        self.log(f"{'DRY RUN - ' if do_dry_run else ''}{climate_entity}: Restored temperature to {self.restore_temp}C")  
        
        self.summary.zones[climate_entity].end_temp = float(current)
        self.summary.zones[climate_entity].completed = True
        self.summary.save()

        if do_dry_run:
            self.job_scheduler.schedule(self.delayed_get_temperature, datetime.now() + timedelta(seconds=30), kwargs={"climate_entity": climate_entity})
            self.job_scheduler.schedule(self.delayed_get_temperature, datetime.now() + timedelta(seconds=60), kwargs={"climate_entity": climate_entity})            
        else:
            self.job_scheduler.schedule(self.delayed_get_temperature, datetime.now() + timedelta(minutes=30), kwargs={"climate_entity": climate_entity})
            self.job_scheduler.schedule(self.delayed_get_temperature, datetime.now() + timedelta(minutes=60), kwargs={"climate_entity": climate_entity})

        # Process the next entity after this one finishes
        self.process_next_zone()

    def delayed_get_temperature(self, kwargs=None):

        climate_entity = kwargs.get("climate_entity")
        if climate_entity is None:
            self.log("No climate entity provided, cannot get delayed temperature.")
            return

        current_temp = self.get_state(climate_entity, attribute="current_temperature")

        record = self.summary.zones[climate_entity].add_end_temperature(float(current_temp))
        self.summary.save()
        self.log(f"{climate_entity}: Delayed temperature: Got Current Temperature after {record.minutes_after_end} minutes: {current_temp}C", level="DEBUG")

    def _create_entity_queue(self, hvac_mode: str = "heat"):
        """
        Create a queue of entities that are in the specified HVAC mode.
        """
        self.active_queue = [e for e in self.full_entity_list if self.get_state(e) == hvac_mode]

    def finalize_day(self):
        """
        Finalize the day by saving the summary and clearing the cache.
        """
        self.log_summary()

        ## Clear the cache for the next day
        self.summary.clear()

        # clear the summary memory structure
        self.summary = DailySummary.load("daily:summary")

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
        
    def terminate(self):
        #not using this for now
        pass

