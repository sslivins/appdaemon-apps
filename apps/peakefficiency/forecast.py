from datetime import datetime, timezone
from dataclasses import dataclass, asdict, fields
import requests
import math
import statistics
import json
from pydantic import BaseModel
from typing import Optional

class ForecastDailySummary(BaseModel):
    forecast_start_time: Optional[datetime]  # Start time of the forecast period
    forecast_end_time: Optional[datetime]  # End time of the forecast period
    latitude: Optional[float]  # Latitude of the location
    longitude: Optional[float]  # Longitude of the location
    min_temperature: Optional[float]  # Minimum temperature during the day
    max_temperature: Optional[float]  # Maximum temperature during the day
    avg_temperature: Optional[float]  # Average temperature during the day
    avg_daytime_temperature: Optional[float]  # Average temperature during daylight hours
    avg_nighttime_temperature: Optional[float]  # Average temperature during nighttime hours
    total_solar_radiation: Optional[float]  # Total solar radiation during the day (kWh/m²)
    avg_humidity: Optional[float]  # Average humidity during the day
    

class ForecastSummary:
    def __init__(self, app, lat, lon, hours=24):
        self.app = app
        self.lat = lat
        self.lon = lon
        self.forecast_data = self._get_hourly_forecast(lat, lon, hours=hours)
        
    def get_forecast_data(self, start_time=None, end_time=None):
        """
        Returns the forecast data.
        """
               
        return self.forecast_data
        
    def _get_hourly_forecast(self, lat, lon, hours=6):
        
        #calculate forecast days based on hours
        forecast_days = math.ceil(hours / 24 + 1)
        
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
        
        #get the next 'hours' hours of forecast data starting from now
        now = datetime.now().isoformat()
        
        #round current time down to the nearest hour
        now = now[:13] + ":00"        
        start_index = next((i for i, t in enumerate(times) if t >= now), 0)
        times = times[start_index:start_index + hours]
        temps = temps[start_index:start_index + hours]
        humidity = humidity[start_index:start_index + hours]
        radiation = radiation[start_index:start_index + hours]

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

    def _split_daytime_nighttime_hours(self, data):
        """
        Split the data into overnight and daytime hours based on the radiation.
        """
        overnight = []
        daytime = []
        for t, temp, rh, rad in data:
            if rad == 0:  # Radiation is 0 during nighttime
                overnight.append((t, temp, rh, rad))
            else:
                daytime.append((t, temp, rh, rad))
        return overnight, daytime
    
    def summarize(self):
        overnight, daytime = self._split_daytime_nighttime_hours(self.forecast_data)    

        # Calculate daytime and nighttime averages
        if daytime:
            avg_daytime_temperature = statistics.mean(temp for _, temp, _, _ in daytime)
        else:
            avg_daytime_temperature = None

        if overnight:
            avg_nighttime_temperature = statistics.mean(temp for _, temp, _, _ in overnight)
        else:
            avg_nighttime_temperature = None

        # Calculate overall averages
        all_temps = [temp for _, temp, _, _ in self.forecast_data]
        avg_temperature = statistics.mean(all_temps) if all_temps else None
        min_temperature = min(all_temps) if all_temps else None
        max_temperature = max(all_temps) if all_temps else None

        # Calculate total solar radiation
        total_solar_radiation = sum(rad for _, _, _, rad in self.forecast_data)

        # Calculate average humidity
        all_humidity = [rh for _, _, rh, _ in self.forecast_data]
        avg_humidity = statistics.mean(all_humidity) if all_humidity else None

        # Determine forecast start and end times
        forecast_start_time = datetime.fromisoformat(self.forecast_data[0][0]) if self.forecast_data else None
        forecast_end_time = datetime.fromisoformat(self.forecast_data[-1][0]) if self.forecast_data else None

        # Create and return the ForecastDailySummary object
        return ForecastDailySummary(
            forecast_start_time=forecast_start_time,
            forecast_end_time=forecast_end_time,
            latitude=self.lat,
            longitude=self.lon,
            min_temperature=min_temperature,
            max_temperature=max_temperature,
            avg_temperature=avg_temperature,
            avg_daytime_temperature=avg_daytime_temperature,
            avg_nighttime_temperature=avg_nighttime_temperature,
            total_solar_radiation=total_solar_radiation,
            avg_humidity=avg_humidity
        )
    



