class HelperUtils:
    def __init__(self, app):
        self.app  = app
        
    def safe_get_float(self, entity_id, default):
        try:
            return float(self.app.get_state(entity_id))
        except (TypeError, ValueError):
            self.app.log(f"Could not read {entity_id}, using default {default}", level="WARNING")
            return default
        
    def assert_entity_exists(self, entity_id, friendly_name=None, required=True):
        """
        Check if an entity exists in Home Assistant. If it doesn't, log an error and raise a ValueError.
        :param entity_id: The entity ID to check.
        :param friendly_name: Optional friendly name for logging.
        :param required: If True, raise an error if the entity does not exist.
        """
        entity_state = self.app.get_state(entity_id)
        
        if entity_state is not None:
            return
        
        if required:
            friendly_name = friendly_name or entity_id
            self.app.error(f"Entity {friendly_name} ({entity_id}) does not exist in Home Assistant.")
            raise ValueError(f"Entity {friendly_name} ({entity_id}) does not exist in Home Assistant.")
        else:
            self.app.log(f"Entity {friendly_name} ({entity_id}) does not exist in Home Assistant. Proceeding without it.", level="WARNING")