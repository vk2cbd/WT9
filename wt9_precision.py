"""Precision fixed-position slews for WT9 Park and Service.

Uses existing guarded motion and software limits. No tracking changes.
"""
from wt9_antenna import SafetyError
from wt9_service import validate_service_target

FINAL_TOLERANCE_AZ = 0.15
FINAL_TOLERANCE_EL = 0.15
SETTLE_SECONDS = 1.0
MAX_CORRECTIONS = 2

class PositionCancelled(Exception):
    """Operator cancelled a fixed-position move."""

def position_errors(limits, position, az, el):
    return (limits.azimuth_delta_to_target(position.azimuth, az), el - position.elevation)

def precision_fixed_slew(session, az, el, stop_event, site, az_start_tol, el_start_tol,
                         update_callback=None, log_callback=None):
    """Move and verify settled encoder coordinates; return position or raise.

    Caller owns exclusive motion scheduling. Stop events interrupt settling.
    """
    limits = session.config.limits
    validate_service_target(limits, az, el, FINAL_TOLERANCE_AZ, FINAL_TOLERANCE_EL)
    if stop_event.is_set():
        raise PositionCancelled()
    if log_callback is None:
        log_callback = lambda **fields: None
    for attempt in range(MAX_CORRECTIONS + 1):
        if stop_event.is_set():
            raise PositionCancelled()
        if attempt == 0:
            az_speed, el_speed = session.config.az_track_speed, session.config.el_track_speed
            start_az, start_el = az_start_tol, el_start_tol
        else:
            az_speed, el_speed = site.az_slow_speed, site.el_slow_speed
            start_az, start_el = FINAL_TOLERANCE_AZ, FINAL_TOLERANCE_EL
        session.guarded_slew_to(
            az, el, az_speed, el_speed, stop_event,
            start_az, start_el,
            site.az_stop_tolerance_degrees, site.el_stop_tolerance_degrees,
            site.az_slow_speed, site.el_slow_speed,
            site.az_slow_threshold_degrees, site.el_slow_threshold_degrees,
            update_callback,
            apply_az_low_to_high_compensation=False,
        )
        if stop_event.is_set() or stop_event.wait(SETTLE_SECONDS):
            raise PositionCancelled()
        position = session.read_position()
        if update_callback:
            update_callback(position)
        az_error, el_error = position_errors(limits, position, az, el)
        log_callback(attempt=attempt, requested_az=az, requested_el=el,
                     measured_az=position.azimuth, measured_el=position.elevation,
                     az_error=az_error, el_error=el_error)
        if abs(az_error) <= FINAL_TOLERANCE_AZ and abs(el_error) <= FINAL_TOLERANCE_EL:
            return position
        if attempt < MAX_CORRECTIONS:
            validate_service_target(limits, az, el, FINAL_TOLERANCE_AZ, FINAL_TOLERANCE_EL)
    raise SafetyError(
        f"Final positioning error after {MAX_CORRECTIONS} corrections: "
        f"AZ {az_error:+.2f} deg, EL {el_error:+.2f} deg "
        f"(allowed +/-{FINAL_TOLERANCE_AZ:.2f}/+/-{FINAL_TOLERANCE_EL:.2f} deg)"
    )
