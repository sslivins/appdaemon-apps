from datetime import time
import hassapi as hass

class PeakEfficiency(hass.Hass):

    def initialize(self):
        self.restore_temp = self.safe_get_float("input_number.away_mode_target_temperature", 13)
        self.heat_to_temp = self.safe_get_float("input_number.away_mode_peak_heat_to_tempearture", 19.5)
        self.heat_duration = 20 * 60  # 20 minutes

        self.full_entity_list = [
            "climate.main_floor",
            "climate.master_bedroom",
            "climate.basement_master",
            "climate.basement_bunk_rooms",
            "climate.ski_room"
        ]
        self.active_queue = []  # Will store entities to run

        # Optional trigger
        self.listen_state(self.start_override, "input_boolean.start_peak_efficiency", new="on")

        # Run daily at 3:00 PM
        self.run_daily(self.start_override, time(15, 0, 0))

        self.log("PeakEfficiency initialized")

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
        self.log(f"Overriding {climate} to {self.heat_to_temp}C for {self.heat_duration // 60} minutes.")

        do_dry_run = self.get_state("input_boolean.peak_efficiency_dry_run") == "on"
        if not do_dry_run:
            self.call_service("climate/set_temperature", entity_id=climate, temperature=self.heat_to_temp)
        else:
            self.log(f"{climate}: Not modifying temperature as Dry Run mode is enabled")

        outside_temp = self.get_state("sensor.condenser_temperature_sensor_temperature")
        current_temp = self.get_state(climate, attribute="current_temperature")

        # Schedule restore after heat_duration
        self.run_in(self.restore_temperature, self.heat_duration, climate=climate, outside_temp=outside_temp, start_temp=current_temp)

    def restore_temperature(self, kwargs):
        climate = kwargs["climate"]
        outside_temp = kwargs["outside_temp"]
        start = kwargs["start_temp"]
        current = self.get_state(climate, attribute="current_temperature")
        do_dry_run = self.get_state("input_boolean.peak_efficiency_dry_run") == "on"
        if not do_dry_run: 
            self.call_service("climate.set_temperature", entity_id=climate, temperature=self.restore_temp)
        else:
            self.log(f"{climate}: Not modifying temperature as Dry Run mode is enabled")

        self.log(f'Restored {climate} to {self.restore_temp}C | Outside: {outside_temp}C | Start: {start}C | End: {current}C')

        # Process the next entity after this one finishes
        self.process_next_climate()
