import unreal
import mrg_callbacks
import kitsu_utils
import threading


# Fire and forget — doesn't block Unreal startup.
# If it fails, uploads reconnect on their own (kitsu_utils.ensure_connected).
thread = threading.Thread(target=kitsu_utils.connect_kitsu, daemon=True)
thread.start()

# Registered after mrg_callbacks' own callback, which waits for running publishes first
unreal.register_python_shutdown_callback(kitsu_utils.disconnect_kitsu)
