from datetime import time
import hassapi as hass
from datetime import timedelta
import json
from datetime import datetime, timezone
from dataclasses import dataclass, asdict, fields
import requests
import math
import statistics

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
OUTDOOR_TEMPERATURE_SENSOR = "sensor.condenser_temperature_sensor_temperature"
AWAY_TARGET_TEMP = "input_number.away_mode_target_temperature"
AWAY_PEAK_HEAT_TO_TEMP = "input_number.away_mode_peak_heat_to_tempearture"
AWAY_MODE_ENABLED = "input_boolean.home_away_mode_enabled"


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
        
        self.schedule_handle = None
        #make sure helpers exist, otherwise error out
        #check if the timer exists
        self.assert_entity_exists(RESTORE_TEMPERATURE_TIMER, "Peak Efficiency Restore Timer")
        self.assert_entity_exists(CLIMATE_STATE, "Peak Efficiency Climate State Buffer")
        self.assert_entity_exists(AWAY_MODE_ENABLED, "Away Mode Enabled")
        
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
            #default start time is 3pm
            run_at = DEFAULT_RUN_AT_TIME            

        if self.schedule_handle is not None:
            self.log(f"PeakEfficiency already scheduled for {self.schedule_handle}, cancelling it.")
            self.cancel_timer(self.schedule_handle)
            
        #only run this while in away mode
        if self._is_away_mode_enabled():
            self.schedule_handle = self.run_daily(self.start_heat_soak, run_at)
      
            run_at_am_pm = run_at.strftime("%I:%M %p")
            self.log(f"PeakEfficiency will run today at {run_at_am_pm}.", level="INFO")
        else:
            self.log(f"PeakEfficiency will not run today because Away Mode is not enabled.", level="INFO")

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
        

    def start_heat_soak(self, entity=None, attribute=None, old=None, new=None, kwargs=None):
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
        
    def _is_away_mode_enabled(self):
        """
        Check if the home/away mode is enabled.
        """
        return self.get_state(AWAY_MODE_ENABLED) == "on"
        
    def terminate(self):
        #not using this for now
        pass


class ForecastSummary:
    def __init__(self, app, lat, lon):
        self.app = app
        self.lat = lat
        self.lon = lon
        self.forecast_data = self._get_hourly_forecast(lat, lon, hours=48)
        
    def get_forecast_data(self, start_time=None, end_time=None):
        """
        Returns the forecast data.
        """
               
        return self.forecast_data
        
    def _get_hourly_forecast(self, lat, lon, hours=6):
        
        #calculate forecast days based on hours
        forecast_days = math.ceil(hours / 24)
        
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m,relative_humidity_2m,shortwave_radiation",
            "forecast_days": forecast_days,
            "timezone": "auto"
        }

        try:
            # Check if the API is reachable
            response = requests.get(url, params=params)
            response.raise_for_status()
            
            data = response.json()            
        except requests.exceptions.RequestException as e:
            self.error(f"Error fetching forecast data: {e}")
            return []
        except json.JSONDecodeError as e:
            self.error(f"Error decoding JSON response: {e}")
            return []
        except Exception as e:
            self.error(f"Unexpected error: {e}")
            return []
        
        # Check if the response contains the expected data
        if "hourly" not in data or not data["hourly"]:
            self.error("Invalid response structure from Open-Meteo API.")
            return []
        
        times = data["hourly"]["time"]
        temps = data["hourly"]["temperature_2m"]
        humidity = data["hourly"]["relative_humidity_2m"]
        radiation = data["hourly"]["shortwave_radiation"]

        # Return a list of tuples for unpacking
        return list(zip(times, temps, humidity, radiation))[:hours]  
    
    
    def warmest_hours(self, minutes):
        """
        within a given forecast, ensuring the period is within the current day and 
        starts after the current time (not in the past).
            - The identified period must start after the current time and end within 
              the current day.
        Args:
            forecast (list of tuples): A list of forecast data where each tuple contains 
                (time, temperature, humidity, radiation). Only the time and temperature 
                are used in this function.
            minutes (int): The duration in minutes for which the warmest period is to 
                be calculated. This value is rounded up to the nearest full hour.
        Returns:
            tuple: A tuple containing:
                - best_start_time (datetime): The starting time of the warmest period.
                - block_size (int): The number of hours in the warmest period.
        Raises:
            ValueError: If the forecast data is shorter than the required window size.
        Notes:
            - The function calculates the sum of temperatures for consecutive hours 
              and identifies the period with the highest sum.
            - The forecast data must be in ISO 8601 format for the time values.
        """
        forecast = self.forecast_data
        block_size = math.ceil(minutes / 60 )  # round up to full hours
        if len(forecast) < block_size:
            raise ValueError("Forecast data too short for the requested window")

        max_sum = float('-inf')
        best_start_time = None

        # forecast is list of tuples: (time, temp, humidity, radiation)
        # we only need the first two elements of each tuple
        forecast_temp = [(datetime.fromisoformat(t), temp) for t, temp, _, _ in forecast]
        for i in range(len(forecast) - block_size + 1):
            window = forecast_temp[i:i + block_size]
            temp_sum = sum(temp for _, temp in window)

            #find the max sum of the block but must be after current time and cannot exceed current day
            if window[0][0] < datetime.now() or window[-1][0] > datetime.now() + timedelta(days=1):
                self.app.log(f"Skipping window {window} as it is not within the current day or starts in the past. ({datetime.now()} < {window[0][0]}", level="DEBUG")
                continue
            
            if temp_sum > max_sum:
                max_sum = temp_sum
                best_start_time = window[0][0]  # timestamp of the first hour

        return best_start_time, block_size          

    def _filter_overnight_hours(self, data):
        """
        Filter for hours between sunset and wake-up (e.g. 8pm to 8am).
        Adjust as needed.
        """
        overnight = []
        for t, temp, rh, rad in data:
            hour = datetime.fromisoformat(t).hour
            if hour >= 20 or hour <= 8: # 8 PM to 8 AM
                overnight.append((t, temp, rh, rad))
        return overnight

    def summarize(self):
        overnight = self._filter_overnight_hours(self.forecast_data)

        if not overnight:
            self.app.log("No overnight data available for summary.")
            return {}

        temps = [temp for _, temp, _, _ in overnight]
        humidities = [rh for _, _, rh, _ in overnight]
        radiation = [rad for _, _, _, rad in overnight]

        min_temp = min(temps)
        avg_temp = statistics.mean(temps)
        avg_humidity = statistics.mean(humidities)
        avg_radiation = statistics.mean(radiation)

        duration_below_zero = sum(1 for t in temps if t < 0)

        # Find time of min temperature
        min_temp_index = temps.index(min_temp)
        min_temp_time = overnight[min_temp_index][0]
        min_temp_hour = datetime.fromisoformat(min_temp_time).hour

        return {
            "min_forecast_temp_overnight": min_temp,
            "avg_forecast_temp_overnight": avg_temp,
            "avg_humidity_overnight": avg_humidity,
            "avg_radiation_overnight": avg_radiation,
            "duration_below_zero": duration_below_zero,
            "hour_of_min_temp": min_temp_hour
        }
