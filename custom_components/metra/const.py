"""Constants for the Metra integration."""
DOMAIN = "metra"
STATIC_URL = "/metra_static"      # engine map icons, served from custom_components/metra/www
TEMPLATE_FILE = "metra.jinja"     # installed into <config>/custom_templates/ at startup
CACHE_DIR = ".metra_cache"        # GTFS download + parsed index, under <config>/
LEGACY_CACHE_DIR = ".metra_mqtt"  # pre-HACS name; renamed to CACHE_DIR once
