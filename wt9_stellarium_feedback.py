"""Read-only Stellarium Telescope Control feedback for WT9.

Two configurable TCP listeners: East 10001, West 10002. Incoming telescope
commands are never interpreted or forwarded to the antenna controllers.
Requires astropy for an accurate AltAz -> ICRS/J2000 transformation.
"""
from __future__ import annotations

import math
import socket
import struct
import threading
import time
from datetime import datetime, timezone

PORTS = {"East": 10001, "West": 10002}


def encode_position(ra_hours, dec_degrees, timestamp_us=None):
    if not (math.isfinite(ra_hours) and math.isfinite(dec_degrees) and -90 <= dec_degrees <= 90):
        raise ValueError("Invalid equatorial coordinates")
    ra = int((ra_hours % 24.0) / 24.0 * (1 << 32)) & 0xffffffff
    dec = int(dec_degrees / 90.0 * (1 << 30))
    stamp = int(time.time() * 1e6) if timestamp_us is None else int(timestamp_us)
    return struct.pack("<HHqIii", 24, 0, stamp, ra, dec, 0)


def horizontal_to_icrs(azimuth, elevation, latitude, longitude, when):
    """Transform encoder Alt/Az to ICRS using astropy (no refraction)."""
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord
    from astropy.time import Time
    import astropy.units as u

    location = EarthLocation(lat=latitude*u.deg, lon=longitude*u.deg, height=0*u.m)
    frame = AltAz(obstime=Time(when), location=location, pressure=0*u.hPa)
    coord = SkyCoord(az=azimuth*u.deg, alt=elevation*u.deg, frame=frame).icrs
    return coord.ra.hour, coord.dec.deg


class StellariumFeedback:
    """Publishes measured antenna positions only, never accepts commands."""
    def __init__(self, latitude, longitude, bind_host='127.0.0.1'):
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        self.bind_host = bind_host
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.positions = {}
        self.listeners = []
        self.threads = []

    def start(self):
        # Bind both sockets before starting threads; clean up on any failure.
        try:
            for name, port in PORTS.items():
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((self.bind_host, port))
                sock.listen(2)
                sock.settimeout(0.5)
                self.listeners.append(sock)
                thread = threading.Thread(target=self._serve, args=(name, sock),
                                          daemon=True, name="Stellarium-" + name)
                self.threads.append(thread)
            for thread in self.threads:
                thread.start()
        except Exception:
            self.close()
            raise

    def update(self, name, azimuth, elevation):
        if name not in PORTS:
            return
        if not (math.isfinite(azimuth) and math.isfinite(elevation) and -90 <= elevation <= 90):
            return
        with self.lock:
            self.positions[name] = (azimuth, elevation, datetime.now(timezone.utc), time.monotonic())

    def clear(self, name=None):
        with self.lock:
            if name is None:
                self.positions.clear()
            else:
                self.positions.pop(name, None)

    def _serve(self, name, listener):
        while not self.stop_event.is_set():
            try:
                conn, _addr = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                conn.settimeout(0.5)
                while not self.stop_event.is_set():
                    with self.lock:
                        entry = self.positions.get(name)
                    if entry and time.monotonic() - entry[3] <= 5.0:
                        try:
                            ra, dec = horizontal_to_icrs(
                                entry[0], entry[1], self.latitude, self.longitude, entry[2])
                            conn.sendall(encode_position(ra, dec))
                        except (OSError, ValueError, ImportError):
                            break
                        except Exception:
                            # Bad transform must not affect WT9 antenna operations.
                            break
                    try:
                        # Drain incoming GoTo packets WITHOUT executing them.
                        data = conn.recv(4096)
                        if not data:
                            break
                    except socket.timeout:
                        pass
                    except OSError:
                        break
                    self.stop_event.wait(0.5)

    def close(self):
        self.stop_event.set()
        for sock in self.listeners:
            try:
                sock.close()
            except OSError:
                pass
        for thread in self.threads:
            if thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=1.5)
