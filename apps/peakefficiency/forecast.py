from datetime import datetime, timezone
from dataclasses import dataclass, asdict, fields
import requests
import math
import statistics
import json
from pydantic import BaseModel
from typing import Optional

class ForecastDailySummary(BaseModel):
    forcast_start_time: Optional[datetime]
    forcast_end_time: Optional[datetime]
    latitude: Optional[float]
    longitude: Optional[float]
    min_forecast_temp_overnight: Optional[float]
    avg_forecast_temp_overnight: Optional[float]
    avg_humidity_overnight: Optional[float]
    avg_radiation_overnight: Optional[float]
    duration_below_zero: Optional[int]
    hour_of_min_temp: Optional[int]
    

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

            #only look at today's forecast
            if window[0][0].date() != datetime.now().date():
                continue

            if temp_sum > max_sum:
                max_sum = temp_sum
                best_start_time = window[0][0]  # timestamp of the first hour

        #if the best time is in the past then return None
        if best_start_time is not None and best_start_time < datetime.now():
            self.app.log(f"Best start time is {best_start_time}, which has passed")
            return None, block_size

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
        
        return ForecastDailySummary(
            forcast_start_time=overnight[0][0],
            forcast_end_time=overnight[-1][0],
            latitude=self.lat,
            longitude=self.lon,
            min_forecast_temp_overnight=min_temp,
            avg_forecast_temp_overnight=avg_temp,
            avg_humidity_overnight=avg_humidity,
            avg_radiation_overnight=avg_radiation,
            duration_below_zero=duration_below_zero,
            hour_of_min_temp=min_temp_hour
        )
