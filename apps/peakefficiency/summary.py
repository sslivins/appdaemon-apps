
from diskcache import Cache
from pydantic import BaseModel, PrivateAttr, ConfigDict
from typing import Type, TypeVar
from typing import List, Dict, Optional, Any
from datetime import timedelta, datetime
import os
from os.path import exists
from forecast import ForecastSummary, ForecastDailySummary
import json
import csv

T = TypeVar("T", bound="PersistentBase")

class PersistentBase(BaseModel):
    _cache: Cache = PrivateAttr()
    _cache_key: str = ""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def __init__(self, cache_path: str, cache_key: str = "", **data):
        """
        Initialize the PersistentBase with a dynamic cache path and optional cache key.
        :param cache_path: The path to the cache directory.
        :param cache_key: The key to use for storing/retrieving data in the cache.
        :param data: Additional data to initialize the model.
        """
        super().__init__(**data)
        self._cache = Cache(cache_path)
        self._cache_key = cache_key

    def save(self):
        data = self.model_dump()
        self._cache.set(self._cache_key, data)

    @classmethod
    def load(cls, cache_path: str, cache_key: str):
        """
        Load an instance from the cache.
        :param cache_path: The path to the cache directory.
        :param cache_key: The key to use for retrieving data from the cache.
        :return: An instance of the class with data loaded from the cache.
        """
        cache = Cache(cache_path)
        data = cache.get(cache_key)
        if data:
            instance = cls(cache_path=cache_path, cache_key=cache_key, **data)
        else:
            instance = cls(cache_path=cache_path, cache_key=cache_key)
        return instance

    def clear(self):
        self._cache.delete(self._cache_key)

   
class TemperatureRecord(BaseModel):
    temperature: float
    timestamp: datetime
    seconds_after_end: float
    

class UnplannedHvacAction(BaseModel):
    hvac_action: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    duration: Optional[int] = None  # Duration in seconds
    completed: bool = False  # Flag to indicate if the action has been completed

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
    unplanned_hvac_actions: List[UnplannedHvacAction] = []  # List of unplanned HVAC events (heating or cooling came on outside of the schedule)

    def add_end_temperature(self, temperature: float, timestamp: datetime = None):
        """
        Add a temperature record to the list, including the time difference from end_time.
        """
        if self.end_time is None:
            raise ValueError("end_time must be set before adding temperature records.")
        
        timestamp = timestamp or datetime.now()

        time_difference = (timestamp - self.end_time).total_seconds()  # Calculate time difference in minutes
        record = TemperatureRecord(
            temperature=temperature,
            timestamp=timestamp,
            seconds_after_end=time_difference
        )
        self.temperature_records.append(record)

        return record
    
    def add_unplanned_hvac_action(self, hvac_action: str, start_time: datetime):
        """
        Add an unplanned HVAC action to the list.
        """
        action = UnplannedHvacAction(
            hvac_action=hvac_action,
            start_time=start_time,
        )
        self.unplanned_hvac_actions.append(action)
        
    def finalize_unplanned_hvac_action(self, end_time: datetime = None):
        """
        Finalize an unplanned HVAC action by setting the end time and duration.
        """
        end_time = end_time or datetime.now()
        #get last unplanned hvac action in list and set the end time and duration
        if self.unplanned_hvac_actions:
            action = self.unplanned_hvac_actions[-1]
            if action.completed == False:
                action.end_time = end_time
                action.duration = (end_time - action.start_time).total_seconds()
                action.completed = True  # Mark the action as completed
                self.unplanned_hvac_actions[-1] = action  # Update the last action in the list
            else:
                self.app.log(f"Unplanned HVAC action already completed for {self.zone}.", level="WARNING")
                
class DailySummary(PersistentBase):
    date: Optional[datetime] = None
    forecast: Optional[ForecastDailySummary] = None
    zones: Optional[Dict[str, ZoneSummary]] = {}

    def __init__(self, cache_path: str, cache_key: str = "", **data):
        super().__init__(cache_path=cache_path, cache_key=cache_key, **data)

    def cache_exists(self) -> bool:
        """
        Check if the cache exists for the current date.
        """
        return self._cache.get(self._cache_key) is not None
    
    def get_started_zones(self) -> List[str]:
        """
        Get a list of zones that have started.
        """
        return [zone for zone, summary in self.zones.items()]
    
    def set_start_time(self):
        self.date = datetime.now()
        self.save()

    def set_forecast(self, latitude: float, longitude: float):
        """
        Set the forecast for the current date using latitude and longitude.
        """
        forecastObj = ForecastSummary(self, latitude, longitude)
        self.forecast = forecastObj.summarize()
        self.save()

    def start_zone(self, climate_entity: str, start_time: datetime = None, end_time: datetime = None, start_temp: float = None, outside_temp: float = None):
        """
        Start a zone with the given start and end times.
        """
        run_duration = (end_time - start_time).total_seconds() if end_time else 0

        zone_summary = ZoneSummary(
            zone=climate_entity,
            start_time=datetime.now(),
            end_time=end_time,
            duration=run_duration,
            start_temp=start_temp,
            outside_temp=outside_temp
        )
        
        self.zones[climate_entity] = zone_summary
        self.save()

    def complete_zone(self, climate_entity: str, end_temp: float):
        self.zones[climate_entity].end_temp = float(end_temp)
        self.zones[climate_entity].completed = True
        self.save()

    def start_unplanned_hvac_action(self, climate_entity: str, hvac_action: str, start_time: datetime = None):
        """
        Start an unplanned HVAC action for the specified zone.
        """
        start_time = start_time or datetime.now()
        zone_summary = self.zones.get(climate_entity)
        
        if zone_summary:
            zone_summary.add_unplanned_hvac_action(hvac_action, start_time)
            self.save()
        else:
            raise ValueError(f"Zone '{climate_entity}' not found in summary.")

    def complete_unplanned_hvac_action(self, climate_entity: str, end_time: datetime = None):
        """
        Complete an unplanned HVAC action for the specified zone.
        """
        end_time = end_time or datetime.now()
        zone_summary = self.zones.get(climate_entity)
        
        if zone_summary:
            zone_summary.finalize_unplanned_hvac_action(end_time)
            self.save()
        else:
            raise ValueError(f"Zone '{climate_entity}' not found in summary.")
        
    def add_delay_temperature(self, climate_entity: str, temperature: float, timestamp: datetime = None):
        """
        Add a temperature record to the specified zone, including the time difference from end_time.
        """
        zone_summary = self.zones.get(climate_entity)
        
        if zone_summary:
            record = zone_summary.add_end_temperature(temperature, timestamp)
            self.save()
            return record
        else:
            raise ValueError(f"Zone '{climate_entity}' not found in summary.")

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
        
class CsvSummary(BaseModel):
    date: datetime
    min_temperature: float
    max_temperature: float
    avg_temperature: float
    total_solar_radiation: float
    avg_humidity: float
    heating_duration: int
    heating_start_time: datetime
    heating_end_time: datetime

    @classmethod
    def get_headers(cls) -> List[str]:
        """
        Generate a list of headers based on the field names of the class.
        """
        return [field for field in cls.__fields__.keys()]

    def get_values(self) -> List[Any]:
        """
        Generate a list of values corresponding to the fields of the class.
        """
        return [getattr(self, field) for field in self.__fields__.keys()]
              
class Summary:

    def write_summary_to_csv(self, file_path: str = os.path.join(os.path.dirname(__file__), "summary.csv")):
        """
        Write the summary data to a CSV file. If the file is empty, write the header as well.

        Args:
            file_path (str): The path to the CSV file. Defaults to "summary.csv" in the current file's directory.
        """

        # Ensure the summary exists
        if not self.summary:
            self.log("No summary data available to write to CSV.", level="WARNING")
            return

        # Prepare the data to write
        data_to_write = []  # Replace with the actual data extraction logic from self.summary

        # Check if the file exists
        file_exists = exists(file_path)

        try:
            with open(file_path, mode='a', newline='') as csv_file:
                writer = csv.writer(csv_file)

                # Write the header if the file is empty
                if not file_exists:
                    header = ["Column1", "Column2", "Column3"]  # Replace with actual column names
                    writer.writerow(header)

                # Write the data rows
                writer.writerows(data_to_write)

            self.log(f"Summary data successfully written to {file_path}.", level="INFO")
        except Exception as e:
            self.log(f"Failed to write summary to CSV: {e}", level="ERROR")