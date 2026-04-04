import logging
from pyats.topology import loader
import sys

logging.basicConfig(stream=sys.stdout, level=logging.INFO)

def test_all():
    tb = loader.load('testbed.yaml')
    for name, dev in tb.devices.items():
        print(f"\n--- Testing {name} ({dev.connections.cli.ip}) ---")
        try:
            dev.connect(learn_hostname=True, connection_timeout=15, log_stdout=False)
            print(f"SUCCESS: Connected to {name}")
            dev.disconnect()
        except Exception as e:
            print(f"FAILED: Could not connect to {name}. Error: {e}")

if __name__ == "__main__":
    test_all()
