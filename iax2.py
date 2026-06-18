"""Minimal IAX2 client for receiving audio from a local Allstar/Asterisk node."""
import socket
import struct
import hashlib
import time
import threading
import logging
import random

log = logging.getLogger(__name__)

try:
    import audioop
    def _ulaw2pcm(data: bytes) -> bytes:
        return audioop.ulaw2lin(data, 2)
except ImportError:
    # audioop removed in Python 3.13 — pure-Python fallback
    def _ulaw2pcm(data: bytes) -> bytes:
        out = bytearray(len(data) * 2)
        for i, b in enumerate(data):
            b    = (~b) & 0xFF
            sign = b & 0x80
            exp  = (b >> 4) & 0x07
            mant = b & 0x0F
            s    = ((mant << 3) | 0x84) << exp
            if sign:
                s = -s
            s = max(-32768, min(32767, s))
            struct.pack_into('<h', out, i * 2, s)
        return bytes(out)



class IAX2Client:
    """RX-only IAX2 client.  Connects to a local Asterisk node and emits PCM."""

    # Frame types
    FRAME_DTMF_END   = 0x01
    FRAME_DTMF_BEGIN = 0x0E
    FRAME_VOICE      = 0x02
    FRAME_TEXT       = 0x07
    FRAME_IAX        = 0x06

    # IAX subclasses
    IAX_NEW       = 0x01
    IAX_PING      = 0x02
    IAX_PONG      = 0x03
    IAX_ACK       = 0x04
    IAX_HANGUP    = 0x05
    IAX_REJECT    = 0x06
    IAX_ACCEPT    = 0x07
    IAX_AUTHREQ   = 0x08
    IAX_AUTHREP   = 0x09
    IAX_INVAL     = 0x0A
    IAX_LAGRQ     = 0x0B
    IAX_LAGRP     = 0x0C
    IAX_VNAK      = 0x12
    IAX_CALLTOKEN = 0x28

    # Information elements
    IE_CALLED_NUMBER  = 0x01
    IE_CALLING_NUMBER = 0x02
    IE_CALLING_NAME   = 0x04
    IE_CALLED_CONTEXT = 0x06
    IE_USERNAME       = 0x07
    IE_CAPABILITY     = 0x09
    IE_FORMAT         = 0x0A
    IE_VERSION        = 0x0C
    IE_CHALLENGE      = 0x0F
    IE_MD5_RESULT     = 0x10
    IE_CALLTOKEN      = 0x39

    CODEC_ULAW = 0x0004

    def __init__(self, host: str, port: int, username: str, secret: str,
                 node: str, context: str = 'radio-iaxrpt'):
        self.host     = host
        self.port     = port
        self.username = username
        self.secret   = secret
        self.node     = str(node)
        self.context  = context

        self._sock       = None
        self._src_call   = 1
        self._dst_call   = 0
        self._oseqno     = 0
        self._iseqno     = 0
        self._ts_origin  = 0.0
        self._call_token = None
        self._running    = False
        self._thread     = None
        self._send_lock  = threading.Lock()

        self.state     = 'idle'   # idle | connecting | connected | error
        self.error_msg = ''

        self._audio_cbs = []
        self._state_cbs = []
        self._text_cbs  = []

        # Retransmit buffer: oseqno -> ulaw payload for VNAK recovery
        self._tx_buf   = {}
        # Serialise DTMF sends — only one sequence at a time
        self._dtmf_lock = threading.Lock()

    # ------------------------------------------------------------------ public

    def on_audio(self, cb):
        """Register callback(pcm_bytes) called with 16-bit 8 kHz PCM."""
        self._audio_cbs.append(cb)

    def on_state_change(self, cb):
        """Register callback(state: str, msg: str) for state transitions."""
        self._state_cbs.append(cb)

    def on_text(self, cb):
        """Register callback(msg: str) for TEXT frames from app_rpt.

        app_rpt pushes status via TEXT frames:
          'L node1,node2,...'  — current linked-node list (empty = none)
          'T node STATUS'      — telemetry/status events
          'C node ...'         — CTCSS / other control
        """
        self._text_cbs.append(cb)

    def send_dtmf(self, digits: str, inter_digit: float = 0.0):
        """Send DTMF digits (non-blocking; fires a daemon thread).

        If a previous sequence is still in flight the new one is queued
        behind it via _dtmf_lock so they never interleave.
        """
        if self.state != 'connected':
            raise RuntimeError('Not connected')
        t = threading.Thread(target=self._dtmf_thread,
                             args=(digits, inter_digit), daemon=True)
        t.start()

    def _send_text_frame(self, text: str) -> None:
        """Send one IAX2 TEXT frame (type 0x07) with the given ASCII payload."""
        with self._send_lock:
            seq     = self._oseqno
            src     = self._src_call | 0x8000
            payload = text.encode('ascii') + b'\x00'
            pkt     = struct.pack('>HHIBBBB',
                                  src, self._dst_call, self._ts(),
                                  seq, self._iseqno,
                                  self.FRAME_TEXT, 0x00) + payload
            self._raw_send(pkt)
            self._tx_buf[seq] = payload
            if len(self._tx_buf) > 128:
                del self._tx_buf[min(self._tx_buf)]
            self._oseqno = (self._oseqno + 1) & 0xFF

    def _dtmf_thread(self, digits: str, inter_digit: float):
        """Send each digit as an IAX2 TEXT frame: 'D <node> 0 1 <digit>'.

        iaxRPT does not use IAX2 DTMF frames or in-band audio tones.
        It sends one TEXT frame per digit in this format, which app_rpt
        processes directly without going through the channel DTMF queue.
        """
        with self._dtmf_lock:   # serialise: only one sequence at a time
            try:
                INTER_DIGIT = 0.05  # 50 ms between digits — matches iaxRPT pacing
                for ch in digits:
                    text = f'D {self.node} 0 1 {ch}'
                    self._send_text_frame(text)
                    log.debug(f'IAX2 TEXT sent: {text!r}')
                    time.sleep(INTER_DIGIT)
            except Exception:
                log.exception('IAX2 _dtmf_thread error')

    def connect(self):
        if self._running:
            return
        self._sock       = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(1.0)
        self._src_call   = random.randint(0x1000, 0x7FFF)
        self._dst_call   = 0
        self._oseqno     = 0
        self._iseqno     = 0
        self._ts_origin  = time.time()
        self._call_token = None
        self._running    = True
        self._set_state('connecting')
        self._send_new()
        self._thread = threading.Thread(
            target=self._recv_loop, daemon=True, name='iax2-recv')
        self._thread.start()

    def disconnect(self):
        self._running = False
        if self.state == 'connected':
            try:
                self._send_iax(self.IAX_HANGUP)
            except Exception:
                pass
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._set_state('idle')

    # ----------------------------------------------------------------- helpers

    def _ts(self):
        return int((time.time() - self._ts_origin) * 1000) & 0xFFFFFFFF

    def _set_state(self, state, msg=''):
        self.state     = state
        self.error_msg = msg
        for cb in self._state_cbs:
            try:
                cb(state, msg)
            except Exception:
                pass

    def _ie(self, t, data):
        if isinstance(data, str):
            data = data.encode()
        return struct.pack('BB', t, len(data)) + data

    def _ie_short(self, t, v):
        return struct.pack('>BBH', t, 2, v)

    def _ie_int(self, t, v):
        return struct.pack('>BBI', t, 4, v)

    def _raw_send(self, data):
        if self._sock:
            self._sock.sendto(data, (self.host, self.port))
            log.debug(f'IAX2 sendto {len(data)}B → {self.host}:{self.port}')

    def _send_iax(self, subclass, ies=b''):
        with self._send_lock:
            src = self._src_call | 0x8000
            pkt = struct.pack('>HHIBBBB',
                              src, self._dst_call, self._ts(),
                              self._oseqno, self._iseqno,
                              self.FRAME_IAX, subclass) + ies
            self._raw_send(pkt)
            # ACK / PONG / LAGRP must not bump the outbound sequence counter
            if subclass not in (self.IAX_ACK, self.IAX_PONG, self.IAX_LAGRP):
                self._oseqno = (self._oseqno + 1) & 0xFF

    def _send_new(self):
        ies  = self._ie_short(self.IE_VERSION,       2)
        ies += self._ie(self.IE_CALLED_NUMBER,       self.node)
        ies += self._ie(self.IE_CALLING_NUMBER,      '0')
        ies += self._ie(self.IE_CALLING_NAME,        self.username)
        ies += self._ie(self.IE_CALLED_CONTEXT,      self.context)
        ies += self._ie_int(self.IE_FORMAT,          self.CODEC_ULAW)
        ies += self._ie_int(self.IE_CAPABILITY,      self.CODEC_ULAW)
        ies += self._ie(self.IE_USERNAME,            self.username)
        if self._call_token is not None:
            ies += self._ie(self.IE_CALLTOKEN, self._call_token)
        log.debug(f'IAX2 -> NEW node={self.node} user={self.username} '
                  f'context={self.context} token={self._call_token is not None}')
        self._send_iax(self.IAX_NEW, ies)

    def _parse_ies(self, data):
        ies, i = {}, 0
        while i + 1 < len(data):
            t, l   = data[i], data[i + 1]
            ies[t] = data[i + 2: i + 2 + l]
            i     += 2 + l
        return ies

    def _parse_full(self, data):
        if len(data) < 12:
            return None
        sr, dr, ts, oseq, iseq, ftype, sub = struct.unpack_from('>HHIBBBB', data)
        return {
            'src':     sr & 0x7FFF,
            'dst':     dr & 0x7FFF,
            'oseq':    oseq,
            'iseq':    iseq,
            'type':    ftype,
            'sub':     sub,
            'ies':     self._parse_ies(data[12:]) if ftype == self.FRAME_IAX else {},
            'payload': data[12:],
        }

    def _emit_audio(self, raw):
        if not raw:
            return
        try:
            pcm = _ulaw2pcm(bytes(raw))
        except Exception:
            return
        for cb in self._audio_cbs:
            try:
                cb(pcm)
            except Exception:
                pass

    # --------------------------------------------------------------- recv loop

    def _recv_loop(self):
        last_ping = time.time()
        while self._running:
            now = time.time()
            if self.state == 'connected' and now - last_ping > 20:
                self._send_iax(self.IAX_PING)
                last_ping = now

            try:
                data, _ = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    log.error(f'IAX2 recv: {e}')
                break

            if not data:
                continue
            if data[0] & 0x80:
                self._handle_full(data)
            else:
                self._handle_mini(data)

        self._running = False
        if self.state == 'connected':
            self._set_state('idle')

    def _handle_full(self, data):
        f = self._parse_full(data)
        if not f:
            return

        if self._dst_call == 0 and f['src']:
            self._dst_call = f['src']

        self._iseqno = (f['oseq'] + 1) & 0xFF

        ftype, sub, ies = f['type'], f['sub'], f['ies']

        IAX_NAMES = {
            0x01:'NEW', 0x02:'PING', 0x03:'PONG', 0x04:'ACK',
            0x05:'HANGUP', 0x06:'REJECT', 0x07:'ACCEPT',
            0x08:'AUTHREQ', 0x09:'AUTHREP', 0x0A:'INVAL',
            0x0B:'LAGRQ', 0x0C:'LAGRP', 0x28:'CALLTOKEN',
        }
        if ftype != self.FRAME_VOICE:
            log.debug(f'IAX2 <- ftype=0x{ftype:02x} sub=0x{sub:02x}'
                      f'({IAX_NAMES.get(sub,"?") if ftype==self.FRAME_IAX else ""}) '
                      f'src={f["src"]} dst={f["dst"]}')

        if ftype == self.FRAME_IAX:

            if sub == self.IAX_CALLTOKEN:
                # Server wants a call token — reset and retry NEW with the token
                self._call_token = ies.get(self.IE_CALLTOKEN, b'')
                log.debug(f'IAX2: got CALLTOKEN len={len(self._call_token)}, retrying NEW')
                self._dst_call   = 0
                self._oseqno     = 0
                self._iseqno     = 0
                self._ts_origin  = time.time()
                self._send_new()

            elif sub == self.IAX_AUTHREQ:
                log.debug(f'IAX2 AUTHREQ raw IEs ({len(f["payload"])} bytes): {f["payload"].hex()}')
                log.debug(f'IAX2 AUTHREQ parsed IEs: { {hex(k): v.hex() for k, v in ies.items()} }')
                challenge = ies.get(self.IE_CHALLENGE, b'').decode('utf-8', errors='replace')
                log.debug(f'IAX2: got AUTHREQ challenge={challenge!r}')
                md5 = hashlib.md5((challenge + self.secret).encode()).hexdigest()
                log.debug(f'IAX2: AUTHREP md5={md5} challenge={challenge!r}')
                self._send_iax(self.IAX_AUTHREP,
                               self._ie(self.IE_MD5_RESULT, md5))

            elif sub == self.IAX_ACCEPT:
                self._send_iax(self.IAX_ACK)
                self._set_state('connected')
                log.info(f'IAX2: connected to node {self.node}')

            elif sub == self.IAX_REJECT:
                cause = ies.get(0x19, b'').decode('utf-8', errors='replace')
                log.warning(f'IAX2: REJECT cause={cause!r}')
                self._set_state('error', f'Rejected: {cause or "unknown"}')
                self._running = False

            elif sub == self.IAX_HANGUP:
                self._send_iax(self.IAX_ACK)
                self._set_state('idle')
                self._running = False

            elif sub == self.IAX_PING:
                self._send_iax(self.IAX_PONG)

            elif sub == self.IAX_LAGRQ:
                self._send_iax(self.IAX_LAGRP)

            elif sub == self.IAX_VNAK:
                # Retransmit all buffered voice frames from requested sequence
                vnak_seq = f['iseq']
                log.debug(f'IAX2: VNAK seq={vnak_seq}, retransmitting from buffer')
                with self._send_lock:
                    for seq in sorted(self._tx_buf):
                        if ((seq - vnak_seq) & 0xFF) < 64:
                            src = self._src_call | 0x8000
                            pkt = struct.pack('>HHIBBBB',
                                              src, self._dst_call, self._ts(),
                                              seq, self._iseqno,
                                              self.FRAME_VOICE, 0x82) + self._tx_buf[seq]
                            self._raw_send(pkt)

            elif sub == self.IAX_ACK:
                # ACK.iseq is cumulative: acknowledges all oseqno < iseqno
                ack_through = (f['iseq'] - 1) & 0xFF
                with self._send_lock:
                    for seq in [s for s in self._tx_buf
                                if ((s - ack_through) & 0xFF) > 128]:
                        del self._tx_buf[seq]

            elif sub not in (self.IAX_PONG, self.IAX_INVAL):
                log.debug(f'IAX2: unhandled IAX sub=0x{sub:02x}, sending ACK')
                self._send_iax(self.IAX_ACK)

        elif ftype == self.FRAME_VOICE:
            self._send_iax(self.IAX_ACK)
            self._emit_audio(f['payload'])

        elif ftype == self.FRAME_TEXT:
            self._send_iax(self.IAX_ACK)
            msg = f['payload'].rstrip(b'\x00').decode('ascii', errors='replace')
            log.debug(f'IAX2 TEXT rx: {msg!r}')
            for cb in self._text_cbs:
                try:
                    cb(msg)
                except Exception:
                    pass

        else:
            self._send_iax(self.IAX_ACK)

    def _handle_mini(self, data):
        # 2B src call, 2B timestamp, rest = ulaw audio
        if len(data) > 4:
            self._emit_audio(data[4:])
