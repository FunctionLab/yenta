import os

from pathlib import Path


YENTA_JSON_STORE_PATH = Path('./pipeline.json').resolve()
YENTA_ENTRY_POINT = os.environ.get('YENTA_ENTRY_POINT', Path('./main.py'))
YENTA_CONFIG_FILE = os.environ.get('YENTA_CONFIG_FILE', Path('./yenta.config'))
YENTA_LOG_FILE = os.environ.get('YENTA_LOG_FILE', None)

VERBOSE = False

