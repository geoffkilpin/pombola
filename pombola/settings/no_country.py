import re

from .base import *

MAPIT_COUNTRY = 'Global'

COUNTRY_APP = None

MAP_BOUNDING_BOX_NORTH = None
MAP_BOUNDING_BOX_SOUTH = None
MAP_BOUNDING_BOX_EAST = None
MAP_BOUNDING_BOX_WEST = None

ENABLED_FEATURES = make_enabled_features(INSTALLED_APPS, ALL_OPTIONAL_APPS)
