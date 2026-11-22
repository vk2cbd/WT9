"""WT9 Service-position preflight. No hardware commands are issued here."""
import math
from wt9_antenna import SafetyError, clockwise_angle_delta, normalize_degrees

def validate_service_target(limits, az, el, stop_az_tolerance=0.0, stop_el_tolerance=0.0):
    """Reject service coordinates outside software limits or inside stop/margin buffers."""
    if not all(math.isfinite(float(v)) for v in (az, el, stop_az_tolerance, stop_el_tolerance)):
        raise SafetyError("Service coordinates and tolerances must be finite.")
    if stop_az_tolerance < 0 or stop_el_tolerance < 0:
        raise SafetyError("Service tolerances cannot be negative.")
    if not 0.0 <= az < 360.0:
        raise SafetyError("Service azimuth must be in [0, 360) degrees.")
    limits.assert_position_allowed(az, el)
    el_clearance = limits.el_margin + stop_el_tolerance
    if el <= limits.el_min + el_clearance or el >= limits.el_max - el_clearance:
        raise SafetyError("Service elevation is too close to a mechanical software limit.")
    if not limits._az_full_circle():
        az_clearance = limits.az_margin + stop_az_tolerance
        start = normalize_degrees(limits.az_min)
        total = clockwise_angle_delta(start, normalize_degrees(limits.az_max))
        offset = clockwise_angle_delta(start, az)
        if offset <= az_clearance or total - offset <= az_clearance:
            raise SafetyError("Service azimuth is too close to a mechanical software limit.")
