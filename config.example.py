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
ALLSTAR_HOST   = '127.0.0.1'   # Asterisk host (almost always localhost)
ALLSTAR_PORT   = 4569           # IAX2 UDP port
ALLSTAR_USER   = 'iaxrpt'       # peer name from iax.conf
ALLSTAR_SECRET = 'YOUR_SECRET_HERE'
ALLSTAR_NODE   = '00000'        # your local node number
