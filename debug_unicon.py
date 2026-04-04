import logging
from pyats.topology import loader
import sys

# Configure logging to see Unicon internals
logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)
logger = logging.getLogger('unicon')
logger.setLevel(logging.DEBUG)

def debug_connect():
    try:
        # Load the existing testbed
        tb = loader.load('testbed.yaml')
        r1 = tb.devices['r1']
        
        print("\n--- Starting Unicon Connection Attempt for r1 ---")
        # Attempt connection with verbose logging
        r1.connect(
            learn_hostname=True,
            log_stdout=True,  # This will dump the actual serial/ssh interaction
            connection_timeout=30
        )
        print("\n--- Connection Successful! ---")
        
    except Exception as e:
        print(f"\n--- Connection Failed! ---\nError Type: {type(e).__name__}\nError Message: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    debug_connect()
