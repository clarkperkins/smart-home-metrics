import os

# shm.collectors.ecobee builds EcobeeConfig() at import time (as a default argument),
# so importing shm.metrics needs this set
os.environ.setdefault("ECOBEE_CLIENT_ID", "test")
