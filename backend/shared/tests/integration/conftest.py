import os

MQTT_HOST = os.environ.get("MQTT_HOST", "mqtt")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "scud_backend")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "scud_backend_dev_password")
