# WT9 — Two-antenna radio astronomy controller

Current WT9 application version: **v0.1.0** (Git commit hash shown in the GUI).

WT9 is the independent successor to the WT8 development codebase, retaining its Git history and two-antenna control functionality.
Historical WT8 and WT7 development remains available through the predecessor repositories and Git history.

## Install

WT9 is the current application and repository name. The launch module is
`wt9_ubuntu_gui.py`, the default configuration is `wt9.ini`,
and the release series starts at `v0.1.0` (with the checked-out Git
revision shown in the GUI).

### Migrating an existing local installation

The repository contains `wt9.ini`, renamed from the original `wt8.ini`.
The migration does not transfer existing Qt preferences. To launch with the
repository configuration:

```bash
cd ~/WT9
python3 wt9_ubuntu_gui.py --config wt9.ini
```

If you have newer local settings, back up `wt9.ini` before replacing it.

Review the copied controller ports, limits, site coordinates, and calibration
settings before connecting the antenna drives. The new WT9 Qt settings
namespace is independent of the former WT8 settings namespace; re-enter
Network Config and any GUI preferences as needed.


```bash
python3 -m pip install -r requirements.txt
```

On Ubuntu, configure serial permissions, UHD/SoapySDR, B210 support and stable /dev/serial/by-id device paths.

### Persistent serial ports for East and West controllers

Connect both USB serial controllers and identify their stable device paths:

```bash
ls -l /dev/serial/by-id/
```

In `wt9.ini`, set the `port` entry in each antenna section to
the corresponding **full path**, e.g.:

```ini
[antenna:East]
port = /dev/serial/by-id/usb-YOUR_EAST_CONTROLLER_ID

[antenna:West]
port = /dev/serial/by-id/usb-YOUR_WEST_CONTROLLER_ID
```

These are illustrative identifiers, not actual devices. Retain the other
existing INI settings. Verify each device's identity before assigning it to
East or West; do not assume `ttyUSB0` always corresponds to East.
The WT9 status bar resolves these paths to short names such as `ttyUSB0` and
`ttyUSB1` for display, while controller connections continue using the
persistent paths. If a configured by-id device is missing, the status bar
shows `device unavailable`. WT9 does not automatically convert Windows
`COM5` or `COM6` names into Linux device paths. Restart WT9 after editing
the INI file.


## Configure

```bash
nano wt9.ini
```

Copying an existing WT7 INI is supported: missing Service coordinates default to AZ 0°, EL 5°. Each `[antenna:East]` and `[antenna:West]` section can define its own `service_az` and `service_el`. They can also be edited in the Limits dialog. Check limits and mechanical clearance before use.

## Run

```bash
python3 wt9_ubuntu_gui.py --config wt9.ini
```

## Service function

The **Service** button is between **Stop Track** and **Park**. It stops source tracking and drives each connected antenna to its fixed configured service coordinates. It does not track after arrival or disconnect. It uses the existing `SafeAntenna.guarded_slew_to` motion protection and performs a separate preflight that rejects any Service target outside the permitted AZ/EL arc or inside the configured safety margins plus stopping tolerances. If either connected antenna fails preflight, neither begins a Service move.

**STOP ALL** requests cancellation of Service and sends the existing antenna stop commands. Other drive workflows are blocked while Service is active. No software check substitutes for physical clearance or hardware end stops; the initial 0° AZ / 5° EL must be verified safe for both antennas before live use.

## Precision Park and Service (verification update)

Both fixed-position commands now use `wt9_precision.precision_fixed_slew`.
The routine disables direction-dependent azimuth target compensation **only for Park and Service**; tracking retains its existing behaviour. It commands a guarded slew, waits 1 second for settling, reads fresh encoders and checks the *requested* coordinates within ±0.15° on both axes. If necessary it makes at most two additional guarded low-speed corrections, verifying after each. Failure to meet tolerance raises a positioning fault instead of displaying PARKED/SERVICE. The preflight rejects targets near configured limits (margin plus 0.15°); this may reject previously accepted Park coordinates. All motion still uses the established safety controller.

The event log records `PARK_VERIFY` and `SERVICE_VERIFY` with attempt, target, measured coordinates and error. Cancellation is supported by STOP ALL or individual antenna STOP; cancelled operations do not report success. **This has not yet been validated on actual antenna hardware.** Do not use these moves for unattended or personnel-safe maintenance until mechanical clearances, approach directions and stopping distances have been tested.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Hardware tests are required before operational use. The software tests do not establish physical safety.

## Repository contents

The development application imports its active controller, astronomy, calibration,
configuration, safety and voice functionality directly from the maintained modules.
The unused compatibility modules `wt9_protocol.py`, `wt9_safety.py`,
`wt9_tracking.py` and `wt9_calibration.py` were retired in the Stage 2 cleanup.
Their earlier implementations remain available through Git history. External
scripts importing those interfaces must be updated before upgrading.

- `wt9_ubuntu_gui.py`: PyQt5 GUI and Service workflow
- `wt9_antenna.py`: Arduino protocol and guarded motion
- `wt9_service.py`: fixed Service destination preflight
- `wt9_config.py`: INI configuration
- `wt9_b210_power.py`: B210 power measurement
- `tests/`: automated regression tests

The original WT7 application-description PDF remains in WT7. The historical PDF is not part of the WT9 source tree; refer to the WT7 repository for it.

## v0.3 GUI fixed-position integration

- Park and Service now display as the main SOURCE field while active and after verified arrival. The main AZ/EL target shows the common destination when both antennas agree, or `Varies` when their individual INI targets differ. Each antenna card always displays its own fixed AZ/EL target and live signed error during motion.
- Stop Track now interrupts Park and Service as well as source tracking. It requests cancellation and issues hardware stop commands to both antennas, using the same emergency-stop pathway as STOP ALL. A cancelled move is not marked PARKED/SERVICE.
- PARKING and SERVICING states are restored after the prior tracking worker has exited, avoiding stale tracking cleanup overwriting the movement label. SERVICING is recognised as an active motion state for rate readouts.
- Fixed-target display is cleared when stopped or faulted; verified successful arrivals retain the destination display. Source tracking continues to use its existing astronomical target logic.
- New headless Qt GUI tests: `tests/test_wt9_fixed_gui.py`.

**Hardware safety:** Stop Track now issues stop commands for both antennas during Park/Service. Verify physical response, encoder error updates and safe cancellation under supervision before routine use. Software changes and tests have not yet been exercised on real hardware.

## Experimental refreshed GUI (feature/manual-latched-gui-redesign)

**Development version v0.4-dev; NOT validated on live antenna hardware.**
The `main` branch is unchanged. Pre-redesign rollback branch:
`backup/pre-gui-redesign-2026-11-14`.

- Refreshed blue title/header, light-blue panel styling, enlarged window and antenna cards.
- Independent per-antenna **MOMENTARY** (press-and-hold) and **LATCHED** (click to start, click same direction to stop) selectors; startup default is MOMENTARY.
- N/S (EL) and E/W (AZ) may run concurrently, each with an independent guarded jog worker and stop event. Opposite-direction requests wait for the previous axis worker to finish.
- Both axes stop on individual antenna STOP, STOP ALL, disconnect, or mode switch. Automatic Park/Service/tracking commands reject while manual movement is active.
- Existing position/encoder display, AZ/EL error/rate fields, source/HA, Sun/Moon, **LMST**, UTC/local time, timeout, Park/Service, all tracking controls, calibration dialogs, B210 controls and logs are retained.
- No changes to tracking geometry, precision fixed-position algorithms or Park/Service auto-disconnect. One exception: guarded-jog exception logging fixed a pre-existing undefined-variable bug.
- The previous illustrative mockup was conceptual; this branch deliberately retains all existing functions and rearranges only manual controls and visual styling.

### Run the experimental branch

```bash
cd ~/WT9
git pull origin feature/manual-latched-gui-redesign
QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests -v
python3 wt9_ubuntu_gui.py --config wt9.ini
```

**Do not operate the stable and development copies against the same controller simultaneously.** Verify STOP, reversal, software limits, motion timeout and both-axis control under attended conditions before using the development build operationally. For rollback, use your existing `main` checkout, or switch the development checkout to `backup/pre-gui-redesign-2026-11-14` after stopping the application.

### Mockup-alignment layout revision (2026-11-14)

The development branch now uses full-width dark-blue antenna headers, grouped AZ/EL target/error/rate telemetry, larger integrated manual pads, and a side-by-side Event Log / live System Status dashboard. The B210 panel remains available above the bottom dashboard, and the existing separate tracking, configuration, calibration, Sun/Moon, local/UTC/**LMST**, timeout and source displays are retained. This is a presentation change; the guarded motor control and Park/Service auto-disconnect remain in place.

**Validation status:** committed but not yet verified by CI or physical-hardware testing. Run `QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests -v` and visually inspect the GUI before connecting to antenna hardware. The stable `main` branch remains unchanged.

### Full dashboard styling pass

The experimental branch now includes a consistent blue/green/red palette, genuine Qt standard toolbar icons, high-contrast Connect/Track/Stop/Park/Service actions, coloured antenna motion states, green SAFE badges, active manual direction highlights, two-button segmented Momentary/Latched selectors, live axis status indicators, and live system-status indicators. The B210 panel is collapsible rather than removed. LMST and the full original toolbar remain available. Changes are restricted to the development branch; the original `main` and pre-redesign backup remain unchanged.

**Verification caveat:** source changes are committed but have not been visually verified against a rendered Linux GUI or physically tested on the motor controllers. Run the headless tests and inspect the GUI with controllers disconnected first.

### 2026-11-14 GUI overlap repair

The manual control heading, Momentary/Latched segmented selector, direction pad, axis status and help text now occupy separate grid rows. The manual pad has a minimum width and explicit directional button column widths to prevent the overlapping controls seen in the 15:09 Ubuntu screenshot. Toolbar actions are split into two grouped rows, with a separate tracking row; duplicate Unicode icon prefixes have been removed in favour of single Qt standard icons, and button widths are calculated from label text plus icon allowance (including Peak Cal). The dashboard now scrolls on smaller displays instead of clipping bottom panels. An offscreen Qt geometry regression test checks manual direction/mode button overlap.

**Test status:** the changes have been committed but automated GUI tests and visual rendering have not been executed in this session. Run `QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests -v` before any live motor testing. The `main` and backup branches remain unchanged.

### Offline Kokoro voice annunciation (development branch)

WT9 plays pre-rendered East/West state-announcement WAV files through Ubuntu's
`aplay`. The current voice is **Kokoro `bf_emma`**, British English female.
Speech generation is an optional one-time preparation step; **neither Kokoro
nor a Python voice virtual environment is needed at runtime**.

To generate or regenerate the current voice pack:

```bash
cd ~/WT9
sudo apt install alsa-utils python3-venv libsndfile1 espeak-ng
python3 -m venv .kokoro-venv
.kokoro-venv/bin/python -m pip install --upgrade pip
.kokoro-venv/bin/python -m pip install kokoro soundfile
.kokoro-venv/bin/python scripts/build_kokoro_voice.py --voice bf_emma
aplay voice/en_AU_female/west_tracking.wav
```

The folder `voice/en_AU_female/` retains its historical name for runtime
compatibility, even though the voice is British. The generated WAV files are
local and Git-ignored; **do not delete them during repository cleanup**.
Once generated, the optional `.kokoro-venv` can be removed.

The Mute and Volume controls are persisted using Qt QSettings and restored
when the GUI starts. State announcements are delayed briefly to suppress
transient states and obsolete queued announcements are skipped. Voice is
informational only, not a safety interlock; motor commands never wait for
audio. A missing voice pack or `aplay` must not affect motion control.

### Historical code and documentation

The former Tkinter GUI, WT7 design notes and superseded Piper generator were
removed from the active repository in the Stage 1 cleanup. Historical versions
remain recoverable from Git history. The active application is the PyQt5 GUI.
