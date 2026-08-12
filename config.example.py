# Copy this file to config.py and fill in your values.
# config.py is gitignored and will not be committed.

# WebSocket URL for the RX audio monitor (DVSwitch PCM player).
# Set this to the WSS endpoint your reverse proxy exposes.
# Leave as empty string to disable the audio monitor button.
AUDIO_WS_URL = 'wss://yourdomain.com/audio-ws/'

# Allstar / IAX2 settings.
# The app connects to your local Asterisk node as an IAX2 client (like iaxRPT).
# ALLSTAR_NODE is your own node number — the called number in the IAX2 NEW frame.
# Add a peer in /etc/asterisk/iax.conf matching ALLSTAR_USER / ALLSTAR_SECRET.
ALLSTAR_HOST   = '127.0.0.1'   # Asterisk host — set to remote IP if not on this Pi
ALLSTAR_PORT   = 4569           # IAX2 UDP port
ALLSTAR_USER   = 'iaxrpt'       # peer name from iax.conf
ALLSTAR_SECRET = 'YOUR_SECRET_HERE'
ALLSTAR_NODE   = '00000'        # your local node number

# API key — required in the X-Api-Key header for service-restart endpoints.
# Set to any random string; keep it secret.
API_KEY = 'CHANGE_ME'

# Flask listen port.
LISTEN_PORT = 9090

# DVSwitch / MMDVM_Bridge paths and service names.
DVSWITCH_SCRIPT      = '/opt/MMDVM_Bridge/dvswitch.sh'
CONNECT_TGIF_SCRIPT  = '/opt/MMDVM_Bridge/connectTGIF.sh'
CONNECT_BM_SCRIPT    = '/opt/MMDVM_Bridge/connectBM.sh'
STFU_SERVICE         = 'stfu.service'
ANALOG_BRIDGE_SERVICE = 'analog_bridge.service'
MMDVM_SERVICE        = 'mmdvm_bridge.service'

# Port the DVSwitchPlayer WebSocket listens on (used in the browser UI).
DVSWITCHPLAYER_PORT = 8080

# -------------------------
# Trunk Recorder integration
# -------------------------

# API key sent by Trunk Recorder's uploader. Must match the 'key' in TR's config.json.
# Leave empty to accept uploads without authentication (not recommended on public networks).
TR_API_KEY = 'CHANGE_ME'

# Directory to store received audio files. Created automatically on startup.
TR_AUDIO_DIR = '/var/lib/dispatcher/tr-audio'

# Maximum number of calls to keep in memory and on disk (oldest are evicted automatically).
TR_MAX_CALLS = 100

# Optional display labels for each Trunk Recorder system short_name.
# Systems not listed here are auto-registered using their short_name as the label.
TR_SYSTEMS = {
    # 'mysystem': 'My County P25',
    # 'fire':     'Fire Dispatch',
}
