
#!/usr/bin/env python3
"""WT9 PyQt5 antenna controller GUI."""
from __future__ import annotations
import argparse, csv, html, json, math, queue, re, subprocess, threading, time
import ipaddress
from urllib.request import Request, urlopen
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from PyQt5.QtCore import Qt, QSize, QPointF, QTimer, pyqtSignal, QEvent, QSettings
from PyQt5.QtGui import QFont, QPainter, QPen, QColor
from PyQt5.QtWidgets import QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSlider, QStyle, QTabWidget, QTableWidget, QTableWidgetItem, QToolTip, QVBoxLayout, QWidget
from wt9_antenna import Axis, Direction, Position, SafeAntenna, shortest_angle_delta
from wt9_astro import TargetPosition, local_sidereal_time, moon_equatorial, moon_position, source_position
from wt9_b210_power import B210PowerMeter, B210PowerMeterConfig, B210PowerReading
from wt9_config import B210Calibration, B210_CAL_LEVELS_DBM, PowerConfig, ScanConfig, SourceConfig, YFactorConfig, calibrated_dbm_from_dbfs, load_b210_calibration, load_configs, load_power_config, load_scan_config, load_site_config, load_sources, load_yfactor_config, save_b210_calibration, save_configs, save_power_config, save_scan_config, save_site_config, save_sources, save_yfactor_config
from wt9_logging import EventLogger
from wt9_solar import sun_equatorial, sun_position
from wt9_state import AppStateStore, SystemRunState
from wt9_service import validate_service_target
from wt9_precision import precision_fixed_slew, PositionCancelled
from wt9_voice import VoiceAnnunciator
from wt9_stellarium_feedback import StellariumFeedback
APP_VERSION = "v0.1.0"

def application_version():
    """Display the checked-out revision, without requiring Git to launch WT9."""
    try:
        root=Path(__file__).resolve().parent
        result=subprocess.run(
            ['git','-C',str(root),'rev-parse','--short=7','HEAD'],
            capture_output=True,text=True,timeout=2,check=True)
        revision=result.stdout.strip()
        if revision:
            return f'{APP_VERSION} ({revision})'
    except (OSError, subprocess.SubprocessError):
        pass
    return APP_VERSION


def serial_port_display(port: str) -> str:
    """Show the Linux tty name for persistent /dev/serial/by-id links."""
    path = Path(str(port))
    if str(path).startswith('/dev/serial/by-id/'):
        if path.exists():
            return path.resolve().name
        return 'device unavailable'
    return path.name if str(path).startswith('/dev/') else str(port)

def hms(seconds: float) -> str:
    seconds %= 86400.0; h=int(seconds//3600); m=int((seconds%3600)//60); s=int(seconds%60); return f"{h:02d}:{m:02d}:{s:02d}"
def ha_text(hours: float) -> str:
    while hours <= -12: hours += 24
    while hours > 12: hours -= 24
    sign='+' if hours >= 0 else '-'; mins=int(round(abs(hours)*60)); return f"{sign}{mins//60:02d}:{mins%60:02d}"
def lbl(text='', name=''):
    w=QLabel(text); w.setObjectName(name); return w
def bold(text='', size=10):
    w=lbl(text,'bold'); f=QFont(); f.setPointSize(size); f.setBold(True); w.setFont(f); return w
def btn(text, name=''):
    w=QPushButton(text); w.setMinimumSize(QSize(46,28)); w.setObjectName(name); return w
def edit(text, width=70):
    w=QLineEdit(str(text)); w.setFixedWidth(width); return w
def lock_width(widget, sample: str, pad: int = 10):
    width = widget.fontMetrics().horizontalAdvance(sample) + pad
    widget.setMinimumWidth(width); widget.setMaximumWidth(width)
    return widget
RATE_MIN_INTERVAL_SECONDS = 0.15
class Panel(QFrame):
    def __init__(self): super().__init__(); self.setObjectName('panel'); self.setFrameShape(QFrame.StyledPanel)
class AzimuthCompass(QWidget):
    """Read-only azimuth dial: cyan encoder and yellow target, north up."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.actual=None
        self.target=None
        self.zenith_rise_az=None
        self.az_limits=None
        self.setMinimumSize(170, 170)
        self.setMaximumWidth(195)
        self.setToolTip('Cyan: measured antenna azimuth; yellow: commanded target azimuth; magenta: zenith-track rising bearing')

    def set_observer_latitude(self,latitude):
        # For a level horizon, a source culminating at zenith has Dec=latitude.
        # Horizon equation: cos(A_rise)=sin(latitude)/cos(latitude).
        try:
            phi=math.radians(float(latitude))
            value=math.tan(phi)
            self.zenith_rise_az=math.degrees(math.acos(value)) if math.isfinite(value) and -1<=value<=1 else None
        except (ValueError,TypeError,OverflowError):
            self.zenith_rise_az=None
        if self.zenith_rise_az is not None:
            self.setToolTip(f'Cyan: measured AZ; yellow: target AZ; magenta: zenith-rise bearing {self.zenith_rise_az:.2f}° (level horizon)')
        self.update()

    def set_az_limits(self,limits):
        self.az_limits=limits
        self.update()

    def az_accessible(self,bearing):
        limits=self.az_limits
        if limits is None:
            return True
        if limits._az_full_circle():
            return True
        # Use the same clockwise permitted arc as SafetyLimits, but exclude
        # the approach margins at both ends for a conservative visual guide.
        start=float(limits.az_min)%360.0
        span=(float(limits.az_max)-float(limits.az_min))%360.0
        margin=max(0.0,float(limits.az_margin))
        offset=(bearing-start)%360.0
        return margin <= offset <= span-margin if span>2*margin else False

    def set_actual(self, degrees):
        self.actual=degrees if degrees is not None and math.isfinite(degrees) else None
        self.update()

    def set_target(self, degrees):
        self.target=degrees if degrees is not None and math.isfinite(degrees) else None
        self.update()

    def paintEvent(self, event):
        p=QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        side=min(self.width(),self.height())
        p.translate(self.width()/2,self.height()/2)
        p.scale(side/180,side/180)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor('#243956'),1.6))
        p.drawEllipse(QPointF(0,0),62,62)
        # Complementary inner arcs show the effective azimuth envelope.
        # Green is permitted; red includes hard stops and safety margins.
        if self.az_limits is not None:
            for a in range(360):
                allowed=self.az_accessible((a+0.5)%360)
                p.setPen(QPen(QColor('#26964b' if allowed else '#d72d36'),
                              2.5,Qt.SolidLine,Qt.FlatCap))
                t1=math.radians(a)
                t2=math.radians(a+1)
                p.drawLine(QPointF(59*math.sin(t1),-59*math.cos(t1)),
                           QPointF(59*math.sin(t2),-59*math.cos(t2)))
        p.setPen(QPen(QColor('#243956'),1.6))
        for a in range(0,360,30):
            theta=math.radians(a)
            r1=52 if a%90==0 else 56
            p.drawLine(QPointF(r1*math.sin(theta),-r1*math.cos(theta)),
                       QPointF(62*math.sin(theta),-62*math.cos(theta)))
        # Label every actual 30-degree tick. Cardinal directions replace the
        # numeric labels at 0/90/180/270 so each mark is unambiguous.
        p.setPen(QColor('#243956'))
        font=p.font(); font.setBold(True); font.setPointSize(9); p.setFont(font)
        metrics=p.fontMetrics()
        for bearing in range(0,360,30):
            label={0:'N',90:'E',180:'S',270:'W'}.get(bearing,f'{bearing}°')
            theta=math.radians(bearing)
            x=77*math.sin(theta)
            y=-77*math.cos(theta)
            p.drawText(QPointF(x-metrics.horizontalAdvance(label)/2,
                               y+(metrics.ascent()-metrics.descent())/2),label)
        def hand(degrees,color,length,width):
            if degrees is None:
                return
            theta=math.radians(degrees%360)
            tip=QPointF(length*math.sin(theta),-length*math.cos(theta))
            p.setPen(QPen(QColor(color),width,Qt.SolidLine,Qt.RoundCap))
            p.drawLine(QPointF(0,0),tip)
            p.setPen(Qt.NoPen); p.setBrush(QColor(color))
            p.drawEllipse(tip,3.5,3.5)
        if self.zenith_rise_az is not None:
            theta=math.radians(self.zenith_rise_az)
            p.setPen(QPen(QColor('#bd31aa'),3,Qt.SolidLine,Qt.RoundCap))
            p.drawLine(QPointF(48*math.sin(theta),-48*math.cos(theta)),
                       QPointF(63*math.sin(theta),-63*math.cos(theta)))
            p.setPen(QColor('#bd31aa'))
            font=p.font(); font.setPointSize(7); font.setBold(True); p.setFont(font)
            label=f'{self.zenith_rise_az:.1f}°'
            # Place the numeric bearing inside the dial, away from 30° perimeter labels.
            x=37*math.sin(theta); y=-37*math.cos(theta)
            p.drawText(QPointF(x-p.fontMetrics().horizontalAdvance(label)/2,y+4),label)
        hand(self.target,'#e4b500',49,4)
        hand(self.actual,'#00b9c9',43,4)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor('#00b9c9') if self.actual is not None else QColor('#9aa6b2'))
        p.drawEllipse(QPointF(0,0),4,4)
        p.end()

class AntennaCard(Panel):
    jog_pressed=pyqtSignal(str,str); jog_released=pyqtSignal(str,str); stop_clicked=pyqtSignal(str); jog_mode_changed=pyqtSignal(str,str); state_changed=pyqtSignal(str,str)
    def __init__(self,name):
        super().__init__(); self.name=name; self.setMinimumHeight(238); self.jog_buttons={}
        g=QGridLayout(self); g.setContentsMargins(7,6,7,6); g.setHorizontalSpacing(14); g.setVerticalSpacing(5)
        title=bold(name.upper()); title.setObjectName('antennaTitle'); title.setMinimumWidth(72); self.state=lbl('DISCONNECTED','stateStopped'); lock_width(self.state,'DISCONNECTED',18)
        self.az=bold('--.--',18); self.el=bold('--.--',18); lock_width(self.az,'359.99',12); lock_width(self.el,'090.00',12)
        self.az_err=lbl('--.--'); self.el_err=lbl('--.--'); lock_width(self.az_err,'+999.99',10); lock_width(self.el_err,'+999.99',10)
        self.limits=lbl('SAFE','safe'); lock_width(self.limits,'FAULT',18); self.mode=lbl('--'); lock_width(self.mode,'Disconnected',10); self.target=lbl('--.-- / --.--'); lock_width(self.target,'359.99 / 090.00',12); self.az_comp=lbl('AZ comp --','muted'); lock_width(self.az_comp,'AZ comp +9.99',10)
        self.az_rate=lbl('--.-- °/s','muted'); self.el_rate=lbl('--.-- °/s','muted')
        head=QFrame(); head.setObjectName('antennaHeader'); hl=QHBoxLayout(head); hl.setContentsMargins(10,3,10,3)
        hl.addWidget(title); hl.addWidget(self.state); hl.addStretch(1)
        g.addWidget(head,0,0,1,3)
        # Three equal-purpose telemetry columns make use of the full available
        # left-hand area while retaining the existing manual jog pad at right.
        telemetry=QHBoxLayout(); telemetry.setContentsMargins(4,4,4,4); telemetry.setSpacing(10)
        def group(heading):
            panel=QFrame(); panel.setObjectName('telemetryGroup')
            layout=QGridLayout(panel); layout.setContentsMargins(10,7,10,7)
            layout.setHorizontalSpacing(10); layout.setVerticalSpacing(5)
            title=bold(heading,9); title.setObjectName('telemetryHeading')
            layout.addWidget(title,0,0,1,2)
            layout.setColumnStretch(1,1)
            return panel,layout
        current=QFrame(); current.setObjectName('telemetryGroup')
        current_layout=QHBoxLayout(current)
        current_layout.setContentsMargins(8,6,8,6)
        current_layout.setSpacing(8)
        self.compass=AzimuthCompass()
        current_layout.addWidget(self.compass,0,Qt.AlignVCenter)
        readings=QGridLayout(); readings.setContentsMargins(0,0,0,0)
        readings.setHorizontalSpacing(6); readings.setVerticalSpacing(5)
        for row,title,value in [(0,'AZ',self.az),(1,'EL',self.el),
                                (2,'AZ rate',self.az_rate),(3,'EL rate',self.el_rate)]:
            readings.addWidget(lbl(title,'muted'),row,0)
            readings.addWidget(value,row,1)
        current_layout.addLayout(readings,1)
        target_panel,target_grid=group('TARGET & ERROR')
        self.target_az_display=lbl('--.--'); self.target_el_display=lbl('--.--')
        target_grid.addWidget(lbl('Target AZ','muted'),1,0); target_grid.addWidget(self.target_az_display,1,1)
        target_grid.addWidget(lbl('Target EL','muted'),2,0); target_grid.addWidget(self.target_el_display,2,1)
        target_grid.addWidget(lbl('AZ err','muted'),3,0); target_grid.addWidget(self.az_err,3,1)
        target_grid.addWidget(lbl('EL err','muted'),4,0); target_grid.addWidget(self.el_err,4,1)
        target_grid.addWidget(self.az_comp,5,0,1,2)
        controller,controller_grid=group('CONTROLLER & LIMITS')
        self.com_port=lbl('--'); self.az_limits=lbl('--'); self.el_limits=lbl('--')
        controller_grid.addWidget(lbl('Limits','muted'),1,0); controller_grid.addWidget(self.limits,1,1)
        controller_grid.addWidget(lbl('Mode','muted'),2,0); controller_grid.addWidget(self.mode,2,1)
        controller_grid.addWidget(lbl('AZ limits','muted'),3,0); controller_grid.addWidget(self.az_limits,3,1)
        controller_grid.addWidget(lbl('EL limits','muted'),4,0); controller_grid.addWidget(self.el_limits,4,1)
        for panel,stretch in ((current,3),(target_panel,3),(controller,4)):
            telemetry.addWidget(panel,stretch)
        g.addLayout(telemetry,1,0,1,1,Qt.AlignVCenter)
        c=QWidget(); c.setObjectName('manualPad'); c.setMinimumWidth(336)
        cg=QGridLayout(c); cg.setContentsMargins(8,5,8,5); cg.setHorizontalSpacing(6); cg.setVerticalSpacing(3)
        mode=QHBoxLayout(); mode.setSpacing(6)
        self.manual_selector=QComboBox(); self.manual_selector.addItems(['MOMENTARY','LATCHED']); self.manual_selector.hide()
        self.mode_momentary=btn('Momentary','modeSegment'); self.mode_latched=btn('Latched','modeSegment')
        self.mode_momentary.setCheckable(True); self.mode_latched.setCheckable(True)
        self.mode_momentary.setChecked(True)
        self.mode_momentary.clicked.connect(lambda:self.select_manual_mode('MOMENTARY'))
        self.mode_latched.clicked.connect(lambda:self.select_manual_mode('LATCHED'))
        self.manual_selector.currentTextChanged.connect(lambda value:self.jog_mode_changed.emit(self.name,value))
        self.manual_selector.currentTextChanged.connect(self.update_manual_mode_display)
        mode.addStretch(1); mode.addWidget(self.mode_momentary); mode.addWidget(self.mode_latched); mode.addStretch(1)
        cg.addLayout(mode,0,0,1,3)
        for text,direction,row,col in [('EL+',Direction.EL_UP.value,1,1),('AZ-',Direction.AZ_CCW.value,2,0),('AZ+',Direction.AZ_CW.value,2,2),('EL-',Direction.EL_DOWN.value,3,1)]:
            b=btn(text); b.setObjectName('jogButton'); b.setFixedSize(100,34); b.setAutoRepeat(False)
            b.pressed.connect(lambda d=direction:self.jog_pressed.emit(self.name,d))
            b.released.connect(lambda d=direction:self.jog_released.emit(self.name,d))
            self.jog_buttons[direction]=b; cg.addWidget(b,row,col)
        stop=btn('STOP','manualStop'); stop.setFixedSize(100,34); stop.clicked.connect(lambda:self.stop_clicked.emit(self.name)); cg.addWidget(stop,2,1)
        cg.setColumnMinimumWidth(0,100); cg.setColumnMinimumWidth(1,100); cg.setColumnMinimumWidth(2,100)
        g.addWidget(c,1,1,1,1,Qt.AlignCenter)
        g.setColumnStretch(0,5); g.setColumnStretch(1,0); g.setColumnStretch(2,0)
        g.setRowStretch(0,0); g.setRowStretch(1,1)
    def set_controller_config(self,config):
        self.com_port.setText(str(config.port))
        limits=config.limits
        self.compass.set_az_limits(limits)
        self.az_limits.setText(f'{limits.az_min:.1f}° … {limits.az_max:.1f}°')
        self.el_limits.setText(f'{limits.el_min:.1f}° … {limits.el_max:.1f}°')
    def select_manual_mode(self,value):
        self.manual_selector.setCurrentText(value)
        self.update_manual_mode_display(value)
    def update_manual_mode_display(self,value):
        self.mode_momentary.setChecked(value=='MOMENTARY')
        self.mode_latched.setChecked(value=='LATCHED')
    def set_manual_active(self,directions):
        labels={Direction.AZ_CW.value:'AZ+',Direction.AZ_CCW.value:'AZ-',Direction.EL_UP.value:'EL+',Direction.EL_DOWN.value:'EL-'}
        for direction,button in self.jog_buttons.items():
            button.setProperty('jogActive',direction in directions)
            button.style().unpolish(button); button.style().polish(button)
        if directions and self.state.text()!='FAULT': self.set_state('MANUAL '+ ' + '.join(labels[d] for d in directions))
    def set_position(self,pos: Optional[Position]):
        self.az.setText('--.--' if pos is None else f'{pos.azimuth:06.2f}'); self.el.setText('--.--' if pos is None else f'{pos.elevation:05.2f}')
        self.compass.set_actual(None if pos is None else pos.azimuth)
    def set_rates(self,az_rate: Optional[float], el_rate: Optional[float]):
        self.az_rate.setText('--.-- °/s' if az_rate is None else f'{az_rate:+0.2f} °/s')
        self.el_rate.setText('--.-- °/s' if el_rate is None else f'{el_rate:+0.2f} °/s')
    def set_target(self,target: Optional[TargetPosition], pos: Optional[Position], az_comp: Optional[float] = None):
        self.compass.set_target(None if target is None else target.azimuth)
        if not target:
            self.target_az_display.setText('--.--'); self.target_el_display.setText('--.--')
            self.target.setText('--.-- / --.--'); self.az_err.setText('--.--'); self.el_err.setText('--.--'); self.az_comp.setText('AZ comp --'); return
        self.target.setText(f'{target.azimuth:06.2f} / {target.elevation:05.2f}')
        self.target_az_display.setText(f'{target.azimuth:06.2f}'); self.target_el_display.setText(f'{target.elevation:05.2f}')
        if az_comp is None: self.az_comp.setText('AZ comp --')
        else: self.az_comp.setText(f'AZ comp {az_comp:+0.2f}' if abs(az_comp) >= 0.005 else 'AZ comp none')
        if pos: self.az_err.setText(f'{shortest_angle_delta(pos.azimuth,target.azimuth):+0.2f}'); self.el_err.setText(f'{target.elevation-pos.elevation:+0.2f}')
    def set_state(self,text):
        changed=self.state.text()!=text.upper()
        self.state.setText(text.upper()); low=text.lower(); name='stateFault' if any(x in low for x in ['fault','timeout']) else ('stateBusy' if any(x in low for x in ['slew','park','servic','scan','yfactor','manual','connecting']) else ('stateGood' if 'tracking' in low else 'stateStopped'))
        self.state.setObjectName(name); self.state.style().unpolish(self.state); self.state.style().polish(self.state); self.mode.setText('Auto' if text.upper() in ['TRACKING','SLEWING','PARKING','SERVICING'] else text.title())
        if text.upper() not in ['TRACKING','SLEWING','PARKING','SERVICING','SCAN','YFACTOR','MANUAL'] and not text.upper().startswith('MANUAL '):
            self.set_rates(None,None)
        if changed: self.state_changed.emit(self.name,text.upper())
    def set_limits_ok(self,ok):
        self.limits.setText('SAFE' if ok else 'FAULT'); self.limits.setObjectName('safe' if ok else 'faultTag'); self.limits.style().unpolish(self.limits); self.limits.style().polish(self.limits)
class B210Panel(Panel):
    start_clicked=pyqtSignal(); stop_clicked=pyqtSignal(); log_start_clicked=pyqtSignal(); log_stop_clicked=pyqtSignal(); cal_clicked=pyqtSignal()
    def __init__(self,power:PowerConfig):
        super().__init__(); self.power=power; self.setMinimumHeight(154); self.setMaximumHeight(174)
        main=QVBoxLayout(self); main.setContentsMargins(10,8,10,8); main.setSpacing(0)
        top=QHBoxLayout(); top.setSpacing(10)
        params_row=QHBoxLayout(); params_row.setSpacing(7)
        actions_row=QHBoxLayout(); actions_row.setSpacing(7)
        self.status=lbl('SDR RELEASED'); lock_width(self.status,'SDR POWER ON UNCAL',18); self.status.setWordWrap(True); self.status.setObjectName('safe')
        self.a_val=bold('--.-',14); self.b_val=bold('--.-',14); lock_width(self.a_val,'-999.9',12); lock_width(self.b_val,'-999.9',12); self.a_unit=bold('dBFS'); self.b_unit=bold('dBFS')
        self.gain_a=edit(power.gain_db,40); self.gain_b=edit(power.gain_b_db,40); self.freq=edit(f'{power.center_frequency_hz/1_000_000:0.1f}',72); self.rate=edit(f'{power.sample_rate_hz/1000:0.0f}',64); self.bw=edit(f'{power.measurement_bandwidth_hz/1000:0.0f}',64); self.clock=edit(power.clock_source or 'internal',78); self.avg=edit(power.smoothing_samples,40); self.gui_hz=edit(f'{power.update_rate_hz:0.0f}',40)
        def add_channel(row,name,value,unit,gain):
            row.addWidget(bold(name)); row.addWidget(value); row.addWidget(unit); row.addWidget(lbl('Gain','muted')); row.addWidget(gain); row.addWidget(lbl('dB'))
        top.addWidget(bold('B210')); add_channel(top,'CH A',self.a_val,self.a_unit,self.gain_a); add_channel(top,'CH B',self.b_val,self.b_unit,self.gain_b); top.addStretch(1); top.addWidget(self.status,0,Qt.AlignRight)
        for text,widget in [('Freq MHz',self.freq),('Rate ksps',self.rate),('BW kHz',self.bw),('Clock',self.clock),('Avg',self.avg),('GUI Hz',self.gui_hz)]:
            params_row.addWidget(lbl(text)); params_row.addWidget(widget)
        params_row.addStretch(1)
        actions=[('SDR Power On',self.start_clicked,'primary'),('Release SDR',self.stop_clicked,''),('Cal',self.cal_clicked,''),('Start Log',self.log_start_clicked,''),('Stop Log',self.log_stop_clicked,'')]
        for text,sig,name in actions:
            b=btn(text,name); b.clicked.connect(sig.emit); actions_row.addWidget(b)
        actions_row.addStretch(1)
        gap=QWidget(); gap.setFixedHeight(28); bottom_gap=QWidget(); bottom_gap.setFixedHeight(12)
        main.addLayout(top); main.addWidget(gap); main.addLayout(params_row); main.addLayout(actions_row); main.addWidget(bottom_gap)
        self.a_hist=[]; self.b_hist=[]; self.latest_raw_a_dbfs=None; self.latest_raw_b_dbfs=None; self.latest_power_dbfs=None; self.latest_power_b_dbfs=None; self.active_calibrations={}; self.power_sequence=0
    def meter_config(self):
        return B210PowerMeterConfig(center_frequency_hz=int(float(self.freq.text())*1_000_000), sample_rate_hz=int(float(self.rate.text())*1000), measurement_bandwidth_hz=int(float(self.bw.text())*1000), update_rate_hz=float(self.gui_hz.text()), gain_a_db=float(self.gain_a.text()), gain_b_db=float(self.gain_b.text()), clock_source=self.clock.text().strip() or 'internal', device_args=self.power.b210_device_args)
    def save_config(self):
        self.power.center_frequency_hz=int(float(self.freq.text())*1_000_000); self.power.sample_rate_hz=int(float(self.rate.text())*1000); self.power.measurement_bandwidth_hz=int(float(self.bw.text())*1000); self.power.update_rate_hz=float(self.gui_hz.text()); self.power.gain_db=self.gain_a.text(); self.power.gain_b_db=self.gain_b.text(); self.power.smoothing_samples=max(1,int(float(self.avg.text()))); self.power.clock_source=self.clock.text().strip() or 'internal'; return self.power
    def load_active_calibrations(self, config_path: Path, power: PowerConfig) -> dict[str, B210Calibration]:
        calibrations={}
        for channel in ('A','B'):
            cal=load_b210_calibration(config_path,power.center_frequency_hz,power.sample_rate_hz,power.measurement_bandwidth_hz,power.gain_db,power.gain_b_db,channel)
            if len(cal.points_dbfs_by_dbm) >= 2:
                calibrations[channel]=cal
        self.active_calibrations=calibrations
        return calibrations
    def set_reading(self,r:B210PowerReading):
        self.latest_raw_a_dbfs=float(r.power_a_dbfs); self.latest_raw_b_dbfs=float(r.power_b_dbfs); self.power_sequence+=1
        keep=max(1,int(float(self.avg.text() or '1'))); self.a_hist=(self.a_hist+[r.power_a_dbfs])[-keep:]; self.b_hist=(self.b_hist+[r.power_b_dbfs])[-keep:]; aa=sum(self.a_hist)/len(self.a_hist); bb=sum(self.b_hist)/len(self.b_hist)
        self.latest_power_dbfs=aa; self.latest_power_b_dbfs=bb; self.active_calibrations=getattr(self,'active_calibrations',{})
        ma=self.measurement_from_dbfs('East','A',aa,len(self.a_hist)); mb=self.measurement_from_dbfs('West','B',bb,len(self.b_hist))
        self.a_val.setText(f"{float(ma['power_value']):0.1f}"); self.b_val.setText(f"{float(mb['power_value']):0.1f}")
        self.a_unit.setText(str(ma['power_unit'])); self.b_unit.setText(str(mb['power_unit']))
        self.status.setText('SDR POWER ON CAL' if ma['power_calibrated'] or mb['power_calibrated'] else 'SDR POWER ON UNCAL')
    def clear_reading(self,status='SDR RELEASED'):
        self.a_hist=[]; self.b_hist=[]; self.latest_power_dbfs=None; self.latest_power_b_dbfs=None; self.latest_raw_a_dbfs=None; self.latest_raw_b_dbfs=None; self.power_sequence=0
        self.a_val.setText('--.-'); self.b_val.setText('--.-'); self.a_unit.setText('dBFS'); self.b_unit.setText('dBFS'); self.status.setText(status)
    def power_channel_for_antenna(self, antenna_name: str) -> str:
        attrs = object.__getattribute__(self, '__dict__')
        app = attrs.get('app')
        cfg = getattr(app, 'power_config', None) or attrs.get('power') or PowerConfig()
        name = (antenna_name or '').strip().lower()
        if name == 'west': return (cfg.west_channel or 'B').strip().upper() or 'B'
        if name == 'east': return (cfg.east_channel or 'A').strip().upper() or 'A'
        return 'A'
    def current_power_measurement(self, antenna_name: str) -> Optional[dict[str, object]]:
        channel = self.power_channel_for_antenna(antenna_name)
        dbfs = getattr(self, 'latest_power_b_dbfs', None) if channel == 'B' else getattr(self, 'latest_power_dbfs', None)
        if dbfs is None: return None
        return self.measurement_from_dbfs(antenna_name, channel, dbfs, 1)
    def current_raw_power_measurement(self, antenna_name: str, after_sequence: int = -1) -> Optional[dict[str, object]]:
        if self.power_sequence <= after_sequence: return None
        channel = self.power_channel_for_antenna(antenna_name)
        dbfs = getattr(self, 'latest_raw_b_dbfs', None) if channel == 'B' else getattr(self, 'latest_raw_a_dbfs', None)
        if dbfs is None: return None
        return self.measurement_from_dbfs(antenna_name, channel, dbfs, 1)
    def measurement_from_dbfs(self, antenna_name: str, channel: str, dbfs: float, sample_count: int) -> dict[str, object]:
        cal = getattr(self, 'active_calibrations', {}).get(channel)
        value = float(dbfs); unit = 'dBFS'; calibrated = False; extrapolated = False
        if cal:
            value, extrapolated = calibrated_dbm_from_dbfs(cal, float(dbfs))
            unit = 'dBm'; calibrated = True
        return {'power_value': value, 'power_dbfs': float(dbfs), 'power_unit': unit, 'power_channel': channel, 'power_calibrated': calibrated, 'power_extrapolated': extrapolated, 'sample_count': sample_count}
    def log_header(self) -> list[str]:
        return ['utc_time','ch_a_dbfs','ch_a_value','ch_a_unit','ch_a_calibrated','ch_b_dbfs','ch_b_value','ch_b_unit','ch_b_calibrated']

class ScanPlotWidget(QWidget):
    def __init__(self, axis, rows, parent=None):
        super().__init__(parent); self.axis=axis; self.rows=rows; self.summary_text='Fit --'; self.hover_points=[]; self.setMouseTracking(True); self.setMinimumSize(560,340)
    def paintEvent(self,event):
        painter=QPainter(self); painter.setRenderHint(QPainter.Antialiasing)
        w,h=self.width(),self.height(); left,right,top,bottom=58,w-22,22,h-48; self.hover_points=[]
        painter.fillRect(0,0,w,h,QColor('white'))
        points=[(float(r['offset_degrees']),float(r['power_value'])) for r in self.rows if r.get('power_value') is not None]
        if not points:
            self.summary_text='Fit unavailable: no scan data'; painter.drawText(20,40,'No scan data'); return
        points=sorted(points); xs=[p[0] for p in points]; ys=[p[1] for p in points]
        fit=self.fit_gaussian_with_slope(points); fit_points=[]
        if fit:
            for i in range(121):
                x=min(xs)+(max(xs)-min(xs))*i/120.0; fit_points.append((x,self.evaluate_fit(fit,x)))
        all_y=ys+[y for _x,y in fit_points]; minx,maxx=min(xs),max(xs); miny,maxy=min(all_y),max(all_y)
        if minx==maxx: minx-=1; maxx+=1
        if miny==maxy: miny-=0.5; maxy+=0.5
        pad=(maxy-miny)*0.08; miny-=pad; maxy+=pad
        def px(x): return left+(x-minx)/(maxx-minx)*(right-left)
        def py(y): return bottom-(y-miny)/(maxy-miny)*(bottom-top)
        grid=QPen(QColor('#d8d8d8'),1); painter.setPen(grid)
        for i in range(6):
            fx=i/5; x=left+fx*(right-left); y=bottom-fx*(bottom-top)
            painter.drawLine(int(x),top,int(x),bottom); painter.drawLine(left,int(y),right,int(y))
            painter.setPen(QColor('#333333')); painter.drawText(int(x)-18,bottom+18,f'{minx+fx*(maxx-minx):0.1f}'); painter.drawText(4,int(y)+4,f'{miny+fx*(maxy-miny):0.1f}'); painter.setPen(grid)
        painter.setPen(QPen(QColor('#222222'),1)); painter.drawLine(left,bottom,right,bottom); painter.drawLine(left,top,left,bottom)
        if minx <= 0 <= maxx:
            pen=QPen(QColor('#555555'),1); pen.setStyle(Qt.DashLine); painter.setPen(pen); x=int(px(0)); painter.drawLine(x,top,x,bottom); painter.drawText(x+4,top+14,'boresight')
        if fit_points:
            painter.setPen(QPen(QColor('#d62728'),2)); last=None
            for xval,yval in fit_points:
                pnt=(int(px(xval)),int(py(yval)))
                if last: painter.drawLine(last[0],last[1],pnt[0],pnt[1])
                last=pnt
        trace=QPen(QColor('#0057b8'),2); painter.setPen(trace); last=None
        for xval,yval in points:
            pnt=(int(px(xval)),int(py(yval)))
            if last: painter.drawLine(last[0],last[1],pnt[0],pnt[1])
            last=pnt
        painter.setBrush(QColor('#0057b8')); painter.setPen(QPen(QColor('#0057b8'),1))
        for xval,yval in points:
            sx=int(px(xval)); sy=int(py(yval)); painter.drawEllipse(sx-3,sy-3,6,6); self.hover_points.append((sx,sy,xval,yval))
        painter.setPen(QColor('#111111')); painter.drawText((left+right)//2-55,h-12,f'{self.axis.value} offset degrees')
        unit=str(self.rows[-1].get('power_unit','dBFS')); painter.save(); painter.translate(16,(top+bottom)//2+35); painter.rotate(-90); painter.drawText(0,0,unit); painter.restore()
        if fit:
            fwhm=2.35482*fit['sigma']; self.summary_text=f"Boresight error {fit['center']:+0.3f} deg; FWHM {fwhm:0.3f} deg; peak {fit['peak']:0.2f} {unit}; RMS {fit['rms']:0.3f} dB"
        else:
            self.summary_text='Gaussian fit unavailable'
    def mouseMoveEvent(self,event):
        closest=None; best_dist=10**9
        for sx,sy,xval,yval in self.hover_points:
            dist=(event.x()-sx)**2+(event.y()-sy)**2
            if dist<best_dist:
                best_dist=dist; closest=(xval,yval)
        if closest and best_dist <= 100:
            unit=str(self.rows[-1].get('power_unit','dBFS')) if self.rows else 'dBFS'
            QToolTip.showText(event.globalPos(),f'{self.axis.value} {closest[0]:0.2f} deg\n{closest[1]:0.2f} {unit}',self)
        else:
            QToolTip.hideText()
    def leaveEvent(self,event):
        QToolTip.hideText()
        super().leaveEvent(event)
    def fit_gaussian_with_slope(self,points):
        if len(points)<5: return None
        points=sorted(points); xs=[p[0] for p in points]; ys=[p[1] for p in points]; min_x,max_x=min(xs),max(xs); span=max_x-min_x
        if span<=0.0: return None
        peak_x=xs[ys.index(max(ys))]; sigma_min=max(span/30.0,0.02); sigma_max=max(span,sigma_min*2.0); center_start=max(min_x,peak_x-span*0.25); center_stop=min(max_x,peak_x+span*0.25); best=None
        for center in self.fit_range(center_start,center_stop,41):
            for sigma in self.fit_range(sigma_min,sigma_max,50):
                fit=self.solve_linear_fit(points,center,sigma)
                if fit and fit['amplitude']>0.0 and (best is None or fit['sse']<best['sse']): best=fit
        if not best: return None
        for center_width,sigma_factor in ((span*0.08,0.35),(span*0.03,0.18)):
            center_start=max(min_x,best['center']-center_width); center_stop=min(max_x,best['center']+center_width); sigma_start=max(sigma_min,best['sigma']*(1.0-sigma_factor)); sigma_stop=min(sigma_max,best['sigma']*(1.0+sigma_factor))
            for center in self.fit_range(center_start,center_stop,41):
                for sigma in self.fit_range(sigma_start,sigma_stop,41):
                    fit=self.solve_linear_fit(points,center,sigma)
                    if fit and fit['amplitude']>0.0 and fit['sse']<best['sse']: best=fit
        best['rms']=math.sqrt(best['sse']/len(points)); best['peak']=self.evaluate_fit(best,best['center']); return best
    def fit_range(self,start,stop,count):
        if count<=1 or start==stop: return [start]
        return [start+(stop-start)*i/(count-1) for i in range(count)]
    def solve_linear_fit(self,points,center,sigma):
        normal=[[0.0 for _ in range(3)] for _ in range(3)]; rhs=[0.0,0.0,0.0]
        for x,y in points:
            g=math.exp(-0.5*((x-center)/sigma)**2); vals=(1.0,x,g)
            for i in range(3):
                rhs[i]+=vals[i]*y
                for j in range(3): normal[i][j]+=vals[i]*vals[j]
        solution=self.solve_3x3(normal,rhs)
        if solution is None: return None
        baseline,slope,amplitude=solution; sse=0.0
        for x,y in points:
            predicted=baseline+slope*x+amplitude*math.exp(-0.5*((x-center)/sigma)**2); sse+=(y-predicted)**2
        return {'baseline':baseline,'slope':slope,'amplitude':amplitude,'center':center,'sigma':sigma,'sse':sse}
    def solve_3x3(self,matrix,rhs):
        a=[matrix[row][:]+[rhs[row]] for row in range(3)]
        for col in range(3):
            pivot=max(range(col,3),key=lambda row: abs(a[row][col]))
            if abs(a[pivot][col])<1e-12: return None
            a[col],a[pivot]=a[pivot],a[col]; pv=a[col][col]
            for item in range(col,4): a[col][item]/=pv
            for row in range(3):
                if row==col: continue
                factor=a[row][col]
                for item in range(col,4): a[row][item]-=factor*a[col][item]
        return [a[row][3] for row in range(3)]
    def evaluate_fit(self,fit,x):
        return fit['baseline']+fit['slope']*x+fit['amplitude']*math.exp(-0.5*((x-fit['center'])/fit['sigma'])**2)
class SimpleDialog(QDialog):
    def __init__(self, app, title):
        super().__init__(app); self.app=app; self.setWindowTitle(title); self.main=QVBoxLayout(self); self.main.setContentsMargins(12,12,12,12); self.main.setSpacing(8)
    def buttons(self):
        box=QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel); box.accepted.connect(self.accept); box.rejected.connect(self.reject); self.main.addWidget(box); return box
    def edit(self, value, width=90):
        w=edit(value,width); return w
    def to_float(self, widget, name):
        try: return float(widget.text())
        except ValueError: raise ValueError(f'{name} must be numeric')
    def to_int(self, widget, name):
        try: return int(float(widget.text()))
        except ValueError: raise ValueError(f'{name} must be numeric')
    def fail(self, exc): QMessageBox.warning(self,self.windowTitle(),str(exc))

class LimitsDialog(SimpleDialog):
    keys=[('az_min','AZ min'),('az_max','AZ max'),('el_min','EL min'),('el_max','EL max'),('az_margin','AZ margin'),('el_margin','EL margin'),('max_jog_seconds','Max jog sec'),('poll_interval','Poll sec'),('park_az','Park AZ'),('park_el','Park EL'),('service_az','Service AZ'),('service_el','Service EL')]
    def __init__(self,app):
        super().__init__(app,'Limits'); self.fields={}; tabs=QTabWidget(); self.main.addWidget(tabs)
        for name,cfg in app.configs.items():
            page=QWidget(); g=QGridLayout(page); g.setContentsMargins(8,8,8,8); f={}; vals={**cfg.limits.__dict__,'park_az':cfg.park_az,'park_el':cfg.park_el,'service_az':cfg.service_az,'service_el':cfg.service_el}
            for r,(key,label) in enumerate(self.keys): g.addWidget(lbl(label),r,0); f[key]=self.edit(vals[key]); g.addWidget(f[key],r,1)
            self.fields[name]=f; tabs.addTab(page,name)
        self.buttons()
    def accept(self):
        try:
            for name,f in self.fields.items():
                cfg=self.app.configs[name]; cfg.limits.az_min=self.to_float(f['az_min'],'AZ min'); cfg.limits.az_max=self.to_float(f['az_max'],'AZ max'); cfg.limits.el_min=self.to_float(f['el_min'],'EL min'); cfg.limits.el_max=self.to_float(f['el_max'],'EL max'); cfg.limits.az_margin=self.to_float(f['az_margin'],'AZ margin'); cfg.limits.el_margin=self.to_float(f['el_margin'],'EL margin'); cfg.limits.max_jog_seconds=self.to_float(f['max_jog_seconds'],'Max jog'); cfg.limits.poll_interval=self.to_float(f['poll_interval'],'Poll sec'); cfg.park_az=self.to_float(f['park_az'],'Park AZ'); cfg.park_el=self.to_float(f['park_el'],'Park EL'); cfg.service_az=self.to_float(f['service_az'],'Service AZ'); cfg.service_el=self.to_float(f['service_el'],'Service EL'); cfg.limits.assert_position_allowed(cfg.park_az,cfg.park_el); validate_service_target(cfg.limits,cfg.service_az,cfg.service_el,self.app.site.az_stop_tolerance_degrees,self.app.site.el_stop_tolerance_degrees)
            save_configs(self.app.config_path,self.app.configs)
            for name,card in self.app.cards.items():
                card.set_controller_config(self.app.configs[name])
            self.app.set_status('Limits saved.'); super().accept()
        except Exception as exc: self.fail(exc)

class ObserverDialog(SimpleDialog):
    def __init__(self,app):
        super().__init__(app,'Observer'); g=QGridLayout(); self.main.addLayout(g); s=app.site
        self.lat=self.edit(s.latitude); self.lon=self.edit(s.longitude); self.timeout_enabled=QCheckBox('Enable disconnect timeout'); self.timeout_enabled.setChecked(bool(s.timeout_enabled)); self.timeout_min=self.edit(s.timeout_minutes); self.retention=self.edit(s.log_retention_days)
        for r,(label,w) in enumerate([('Latitude',self.lat),('Longitude',self.lon),('Timeout min',self.timeout_min),('Log retention days',self.retention)]): g.addWidget(lbl(label),r,0); g.addWidget(w,r,1)
        g.addWidget(self.timeout_enabled,4,0,1,2); self.buttons()
    def accept(self):
        try:
            self.app.site.latitude=self.to_float(self.lat,'Latitude'); self.app.site.longitude=self.to_float(self.lon,'Longitude'); self.app.site.timeout_enabled=self.timeout_enabled.isChecked(); self.app.site.timeout_minutes=self.to_float(self.timeout_min,'Timeout min'); self.app.site.log_retention_days=max(1,self.to_int(self.retention,'Log retention'))
            save_site_config(self.app.config_path,self.app.site); self.app.refresh_zenith_rise_markers(); self.app.set_status('Observer saved.'); self.app.update_reference(); super().accept()
        except Exception as exc: self.fail(exc)

class TrackingDialog(SimpleDialog):
    def __init__(self,app):
        super().__init__(app,'Tracking'); tabs=QTabWidget(); self.main.addWidget(tabs)
        site=QWidget(); sg=QGridLayout(site); self.site_fields={}
        specs=[('track_interval_seconds','Interval sec'),('az_track_tolerance_degrees','AZ start tol'),('el_track_tolerance_degrees','EL start tol'),('az_stop_tolerance_degrees','AZ stop tol'),('el_stop_tolerance_degrees','EL stop tol'),('az_slow_speed','AZ slow speed'),('el_slow_speed','EL slow speed'),('az_slow_threshold_degrees','AZ slow deg'),('el_slow_threshold_degrees','EL slow deg'),('rate_average_samples','Rate avg samples')]
        for r,(key,label) in enumerate(specs): self.site_fields[key]=self.edit(getattr(app.site,key)); sg.addWidget(lbl(label),r,0); sg.addWidget(self.site_fields[key],r,1)
        tabs.addTab(site,'Global'); self.ant_fields={}
        for name,cfg in app.configs.items():
            page=QWidget(); g=QGridLayout(page); f={}; specs=[('gui_speed','Manual speed'),('az_track_speed','AZ speed'),('el_track_speed','EL speed'),('az_low_to_high_compensation','AZ L-H comp')]
            for r,(key,label) in enumerate(specs): f[key]=self.edit(getattr(cfg,key)); g.addWidget(lbl(label),r,0); g.addWidget(f[key],r,1)
            self.ant_fields[name]=f; tabs.addTab(page,name)
        self.buttons()
    def accept(self):
        try:
            for key,w in self.site_fields.items(): setattr(self.app.site,key,max(1,self.to_int(w,key)) if key.endswith('speed') or key.endswith('samples') else self.to_float(w,key))
            for name,f in self.ant_fields.items():
                cfg=self.app.configs[name]; cfg.gui_speed=self.to_int(f['gui_speed'],'Manual speed'); cfg.az_track_speed=self.to_int(f['az_track_speed'],'AZ speed'); cfg.el_track_speed=self.to_int(f['el_track_speed'],'EL speed'); cfg.az_low_to_high_compensation=self.to_float(f['az_low_to_high_compensation'],'AZ L-H comp')
            save_site_config(self.app.config_path,self.app.site); save_configs(self.app.config_path,self.app.configs); self.app.set_status('Tracking settings saved.'); super().accept()
        except Exception as exc: self.fail(exc)

class B210CalibrationDialog(SimpleDialog):
    LEVELS_DBM=B210_CAL_LEVELS_DBM
    def __init__(self,app):
        super().__init__(app,'B210 Calibration')
        g=QGridLayout(); self.main.addLayout(g)
        self.freq=edit(app.power.freq.text(),74); self.rate=edit(app.power.rate.text(),62); self.bw=edit(app.power.bw.text(),62); self.gain_a=edit(app.power.gain_a.text(),48); self.gain_b=edit(app.power.gain_b.text(),48)
        self.channel=QComboBox(); self.channel.addItems(['A','B'])
        fields=[('Freq MHz',self.freq),('Rate ksps',self.rate),('BW kHz',self.bw),('Gain A',self.gain_a),('Gain B',self.gain_b),('Cal channel',self.channel)]
        for i,(label,widget) in enumerate(fields):
            g.addWidget(lbl(label),i//3,(i%3)*2); g.addWidget(widget,i//3,(i%3)*2+1)
        load_btn=btn('Load'); load_btn.clicked.connect(self.load_calibration); g.addWidget(load_btn,0,6,2,1)
        self.table=QTableWidget(len(self.LEVELS_DBM),4); self.table.setHorizontalHeaderLabels(['Source dBm','CH A dBFS','CH B dBFS','Capture'])
        for row,level in enumerate(self.LEVELS_DBM):
            self.table.setItem(row,0,QTableWidgetItem(str(level)))
            self.table.setItem(row,1,QTableWidgetItem('--'))
            self.table.setItem(row,2,QTableWidgetItem('--'))
            capture=btn('Capture Selected'); capture.clicked.connect(lambda _checked=False,l=level:self.capture_level(l)); self.table.setCellWidget(row,3,capture)
        self.table.setMinimumSize(430,300); self.main.addWidget(self.table)
        self.status=lbl('Select channel, set signal generator level, then capture each row.','faultTag'); self.main.addWidget(self.status)
        row=QHBoxLayout(); save=btn('Save Selected'); close=btn('Close'); save.clicked.connect(self.save_selected); close.clicked.connect(self.accept); row.addWidget(save); row.addStretch(1); row.addWidget(close); self.main.addLayout(row)
        self.load_calibration()
    def set_status(self,text): self.status.setText(text)
    def frequency_hz(self): return int(round(float(self.freq.text())*1_000_000))
    def sample_rate_hz(self): return int(round(float(self.rate.text())*1000))
    def bandwidth_hz(self): return int(round(float(self.bw.text())*1000))
    def selected_channel(self): return 'B' if self.channel.currentText().strip().upper() == 'B' else 'A'
    def _col(self,channel): return 2 if channel == 'B' else 1
    def load_calibration(self):
        try:
            for channel in ('A','B'):
                cal=load_b210_calibration(self.app.config_path,self.frequency_hz(),self.sample_rate_hz(),self.bandwidth_hz(),self.gain_a.text(),self.gain_b.text(),channel)
                col=self._col(channel)
                for row,level in enumerate(self.LEVELS_DBM):
                    value=cal.points_dbfs_by_dbm.get(level)
                    self.table.setItem(row,col,QTableWidgetItem(f'{value:0.2f}' if value is not None else '--'))
            self.set_status('Loaded B210 calibration records.')
        except Exception as exc:
            self.set_status(f'Calibration load fault: {exc}')
    def capture_level(self,level_dbm):
        channel=self.selected_channel()
        power=getattr(self.app.power,'latest_power_b_dbfs',None) if channel == 'B' else getattr(self.app.power,'latest_power_dbfs',None)
        if power is None:
            self.set_status(f'Start B210 power and wait for CH {channel} readings before capture.'); return
        row=list(self.LEVELS_DBM).index(level_dbm); self.table.setItem(row,self._col(channel),QTableWidgetItem(f'{power:0.2f}'))
        self.set_status(f'Captured {level_dbm:d} dBm for CH {channel}: {power:0.2f} dBFS.')
    def points_from_table(self,channel):
        points={}; col=self._col(channel)
        for row,level in enumerate(self.LEVELS_DBM):
            item=self.table.item(row,col); text=(item.text() if item else '').strip()
            if text and text != '--': points[level]=float(text)
        return points
    def save_selected(self):
        try:
            channel=self.selected_channel(); points=self.points_from_table(channel)
            if len(points) < 2:
                self.set_status(f'Capture at least two calibration points for CH {channel} before saving.'); return
            calibration=B210Calibration(self.frequency_hz(),self.sample_rate_hz(),self.bandwidth_hz(),self.gain_a.text().strip(),self.gain_b.text().strip(),channel,points)
            save_b210_calibration(self.app.config_path,calibration)
            self.app.power_config=self.app.power.save_config()
            self.app.power.load_active_calibrations(self.app.config_path,self.app.power_config)
            self.app.event_log.info('B210_CAL_SAVE',frequency_hz=calibration.frequency_hz,sample_rate_hz=calibration.sample_rate_hz,bandwidth_hz=calibration.bandwidth_hz,gain_a=calibration.gain_a_db,gain_b=calibration.gain_b_db,channel=channel,points=len(points))
            self.set_status(f'Saved B210 calibration: CH {channel} {len(points)} points.')
        except Exception as exc:
            self.set_status(f'Calibration save fault: {exc}')


class StellariumSourceReader:
    """Read and validate selected Stellarium object; no legacy import dialog."""
    @staticmethod
    def angle(value, hours=False):
        """Parse an explicitly labelled J2000 sexagesimal coordinate."""
        if not isinstance(value,str):
            raise ValueError('J2000 coordinate is not a sexagesimal string')
        t=value.strip().lower().replace('−','-')
        parts=re.findall(r'[-+]?\\d+(?:\\.\\d+)?',t)
        if not parts or len(parts)>3:
            raise ValueError('Unrecognized coordinate: '+value)
        sign=-1 if t.startswith('-') else 1
        nums=[abs(float(x)) for x in parts]
        result=sign*(nums[0]+(nums[1]/60 if len(nums)>1 else 0)+(nums[2]/3600 if len(nums)>2 else 0))
        if not math.isfinite(result) or (hours and not 0<=result<=24) or (not hours and not -90<=result<=90):
            raise ValueError('Coordinate outside permitted range')
        return result%24 if hours else result

    @staticmethod
    def parse_source(data):
        if not isinstance(data,dict) or not data:
            raise ValueError('No selected object returned by Stellarium')
        # Never substitute apparent-of-date, horizontal, or simulated-time coordinates.
        ra=data.get('raJ2000')
        dec=data.get('decJ2000')
        if ra is None or dec is None:
            raise ValueError('Selected object lacks raJ2000/decJ2000 fields; inspect raw response below')
        name=next((v.strip() for key in ('name','englishName','localizedName','localized-name')
                   if isinstance((v:=data.get(key)),str) and v.strip()),'')
        if not name:
            designations=data.get('designations')
            if isinstance(designations,str):
                name=designations.split(' - ',1)[0].strip()
            elif isinstance(designations,list):
                name=next((v.strip() for v in designations if isinstance(v,str) and v.strip()),'')
        if len(name)>100:
            raise ValueError('Invalid object name (over 100 characters)')
        # Stellarium's getObjectInfo map returns raJ2000 and decJ2000
        # as decimal DEGREES, not hours or sexagesimal strings.
        if isinstance(ra,(int,float)) and not isinstance(ra,bool) and isinstance(dec,(int,float)) and not isinstance(dec,bool):
            ra_deg=float(ra); dec_deg=float(dec)
            if not math.isfinite(ra_deg) or not math.isfinite(dec_deg) or not -360<=ra_deg<=360 or not -90<=dec_deg<=90:
                raise ValueError('J2000 coordinates outside valid range')
            return SourceConfig(name or f'J2000 RA {ra_deg:.6f} Dec {dec_deg:+.6f}',(ra_deg/15.0)%24,dec_deg,0.0)
        if not isinstance(ra,str) or not isinstance(dec,str):
            raise ValueError('Unexpected J2000 coordinate representation; inspect raw response')
        if not re.search(r'[h:]|\\s',ra.lower()) or not re.search(r'[d°:]|\\s',dec.lower()):
            raise ValueError('Ambiguous J2000 coordinate units; inspect raw response')
        ra_hours=StellariumSourceReader.angle(ra,True)
        dec_deg=StellariumSourceReader.angle(dec)
        return SourceConfig(name or f'J2000 RA {ra_hours*15:.6f} Dec {dec_deg:+.6f}',ra_hours,dec_deg,0.0)

    @staticmethod
    def fetch_selection(host='127.0.0.1', port=8090):
        url=f'http://{host}:{port}/api/objects/info?format=json'
        request=Request(url,headers={'Cache-Control':'no-cache, no-store','Pragma':'no-cache'})
        with urlopen(request,timeout=3) as response:
            raw=response.read(65537)
        if len(raw)>65536:
            raise ValueError('Response exceeds 64 KiB')
        data=json.loads(raw.decode('utf-8'))
        if not isinstance(data,dict):
            raise ValueError('Invalid Stellarium object response')
        has_name=any(isinstance(data.get(k),str) and data[k].strip()
                     for k in ('name','englishName','localizedName','localized-name','designations'))
        has_name=has_name or (isinstance(data.get('designations'),list)
                              and any(isinstance(v,str) and v.strip() for v in data['designations']))
        if not has_name:
            try:
                with urlopen(f'http://{host}:{port}/api/objects/info',timeout=3) as response:
                    info_html=response.read(65537)
                if len(info_html)<=65536:
                    match=re.search(r'<h2\\b[^>]*>(.*?)</h2>',info_html.decode('utf-8'),re.I|re.S)
                    if match:
                        display_name=html.unescape(re.sub(r'<[^>]*>','',match.group(1))).strip()
                        if 0<len(display_name)<=100:
                            data['name']=display_name
            except (OSError,UnicodeError,ValueError):
                pass
        return data



class SourcesDialog(SimpleDialog):
    NAME_COL=0; RA_COL=1; DEC_COL=2; AZ_COL=3; EL_COL=4; FLUX_COL=5
    def __init__(self,app):
        super().__init__(app,'Sources'); self.table=QTableWidget(0,6); self.table.setHorizontalHeaderLabels(['Name','RA h','Dec deg','Current AZ','Current EL','Flux 4800 MHz']); self.main.addWidget(self.table)
        for src in app.sources.values(): self.add_row(src)
        row=QHBoxLayout(); self.selected=QComboBox(); self.refresh_selected_items(); self.selected.setCurrentText(app.selected_source_name); add=btn('Add Row'); delete=btn('Delete Row'); add.clicked.connect(self.add_new_source); delete.clicked.connect(self.delete_row); row.addWidget(lbl('Selected')); row.addWidget(self.selected); row.addWidget(add); row.addWidget(delete); row.addStretch(1); self.main.addLayout(row); self.resize(760,420); self.buttons()
        self.table.itemChanged.connect(self.on_item_changed); self.table.itemSelectionChanged.connect(self.on_table_selection_changed); self.selected.currentTextChanged.connect(self.on_selected_changed); self.timer=QTimer(self); self.timer.timeout.connect(self.update_current_positions); self.timer.start(1000); self.update_current_positions()
    def readonly_item(self,value='--'):
        item=QTableWidgetItem(str(value)); item.setFlags(item.flags() & ~Qt.ItemIsEditable); return item
    def add_row(self,src):
        r=self.table.rowCount(); self.table.insertRow(r)
        for c,val in enumerate([src.name,src.ra_hours,src.dec_degrees]): self.table.setItem(r,c,QTableWidgetItem(str(val)))
        self.table.setItem(r,self.AZ_COL,self.readonly_item('--')); self.table.setItem(r,self.EL_COL,self.readonly_item('--')); self.table.setItem(r,self.FLUX_COL,QTableWidgetItem(str(src.flux_4800_mhz)))
    def add_new_source(self):
        self.add_row(SourceConfig('New Source',0,0,0)); self.refresh_selected_items()
    def on_table_selection_changed(self):
        r=self.table.currentRow()
        if r < 0: return
        item=self.table.item(r,self.NAME_COL); name=(item.text() if item else '').strip()
        if name:
            self.selected.setCurrentText(name)
            # setCurrentText does not emit when this row was already selected.
            self.on_selected_changed(name)
    def on_selected_changed(self,name):
        name=name.strip()
        if not name or not self.app.sessions:
            return
        if self.app.tracking_kind or self.app.fixed_target_mode:
            self.app.set_status('Stop tracking or fixed-position movement before selecting another source.')
            return
        try:
            sources=self.read_sources_from_table()
            if name not in sources:
                return
            # Selection is a display/configuration change only, never a slew.
            self.app.sources=sources
            self.app.selected_source_name=name
            self.app.site.selected_source=name
            save_sources(self.app.config_path,sources,name)
            self.app.apply_target(self.app.current_tracking_target('source'))
            self.app.set_status(f'Source selected: {name}. Tracking not started.')
        except Exception as exc:
            self.app.set_status(f'Source selection failed: {exc}')
    def select_source(self):
        self.refresh_selected_items(); selected=self.selected.currentText().strip()
        if not selected:
            self.fail(ValueError('Select a source first.')); return
        self.app.selected_source_name=selected; self.app.site.selected_source=selected; save_sources(self.app.config_path,self.read_sources_from_table(),selected); self.app.sources=self.read_sources_from_table(); self.app.update_reference(); self.app.set_status(f'Source selected: {selected}.')
    def read_sources_from_table(self):
        sources={}
        for r in range(self.table.rowCount()):
            name=(self.table.item(r,self.NAME_COL).text() if self.table.item(r,self.NAME_COL) else '').strip()
            if not name: continue
            sources[name]=SourceConfig(name,float(self.table.item(r,self.RA_COL).text()),float(self.table.item(r,self.DEC_COL).text()),float(self.table.item(r,self.FLUX_COL).text()))
        return sources
    def delete_row(self):
        r=self.table.currentRow()
        if r >= 0: self.table.removeRow(r); self.refresh_selected_items(); self.update_current_positions()
    def refresh_selected_items(self):
        current=self.selected.currentText().strip() if hasattr(self,'selected') else ''
        names=[]
        for r in range(self.table.rowCount()):
            item=self.table.item(r,self.NAME_COL); name=(item.text() if item else '').strip()
            if name: names.append(name)
        if not hasattr(self,'selected'): return
        self.selected.blockSignals(True); self.selected.clear(); self.selected.addItems(names); self.selected.setCurrentText(current if current in names else (names[0] if names else '')); self.selected.blockSignals(False)
    def on_item_changed(self,item):
        if item and item.column() == self.NAME_COL: self.refresh_selected_items()
    def update_current_positions(self):
        for r in range(self.table.rowCount()):
            try:
                name=(self.table.item(r,self.NAME_COL).text() if self.table.item(r,self.NAME_COL) else '').strip() or 'Source'
                ra=float(self.table.item(r,self.RA_COL).text()); dec=float(self.table.item(r,self.DEC_COL).text())
                pos=source_position(name,ra,dec,self.app.site.latitude,self.app.site.longitude)
                self.table.setItem(r,self.AZ_COL,self.readonly_item(f'{pos.azimuth:0.2f}')); self.table.setItem(r,self.EL_COL,self.readonly_item(f'{pos.elevation:0.2f}'))
            except Exception:
                self.table.setItem(r,self.AZ_COL,self.readonly_item('--')); self.table.setItem(r,self.EL_COL,self.readonly_item('--'))
    def accept(self):
        try:
            sources=self.read_sources_from_table()
            if not sources: raise ValueError('At least one source is required')
            selected=self.selected.currentText().strip(); selected=selected if selected in sources else next(iter(sources))
            self.app.sources=sources; self.app.selected_source_name=selected; self.app.site.selected_source=selected; save_sources(self.app.config_path,sources,selected)
            if self.app.sessions and not self.app.tracking_kind and not self.app.fixed_target_mode:
                self.app.apply_target(self.app.current_tracking_target('source'))
            self.app.set_status('Sources saved.'); super().accept()
        except Exception as exc: self.fail(exc)
class CalibrationDialog(SimpleDialog):
    def __init__(self,app):
        super().__init__(app,'Calibration'); self.tabs=QTabWidget(); self.main.addWidget(self.tabs); self.fields={}
        for name,cfg in app.configs.items():
            page=QWidget(); g=QGridLayout(page); p=app.positions.get(name); raw_az='--' if not p else f'{p.raw_azimuth:0.2f}'; raw_el='--' if not p else f'{p.raw_elevation:0.2f}'
            az=self.edit(cfg.calibration.az_offset); el=self.edit(cfg.calibration.el_offset); self.fields[name]=(az,el)
            rows=[('Raw AZ',lbl(raw_az)),('Raw EL',lbl(raw_el)),('AZ offset',az),('EL offset',el)]
            for r,(label,w) in enumerate(rows): g.addWidget(lbl(label),r,0); g.addWidget(w,r,1)
            self.tabs.addTab(page,name)
        self.status=lbl(''); self.main.addWidget(self.status)
        row=QHBoxLayout(); manual=btn('Calibrate Manual'); target=btn('Calibrate From Target'); apply=btn('Apply Offsets'); close=btn('Close')
        manual.clicked.connect(lambda:self.set_status('Edit offsets directly, then press Apply Offsets.'))
        target.clicked.connect(self.calibrate_from_target); apply.clicked.connect(self.apply_offsets); close.clicked.connect(self.reject)
        for b in [manual,target,apply]: row.addWidget(b)
        row.addStretch(1); row.addWidget(close); self.main.addLayout(row)
    def current_name(self): return self.tabs.tabText(self.tabs.currentIndex())
    def set_status(self,text): self.status.setText(text)
    def calibrate_from_target(self):
        name=self.current_name(); pos=self.app.positions.get(name); target=self.app.current_target
        if not pos or not target: self.set_status('Current antenna position and target are required.'); return
        az_off=(target.azimuth-pos.raw_azimuth+540.0)%360.0-180.0; el_off=target.elevation-pos.raw_elevation
        az,el=self.fields[name]; az.setText(f'{az_off:0.6f}'); el.setText(f'{el_off:0.6f}'); self.apply_offsets()
    def apply_offsets(self):
        try:
            for name,(az,el) in self.fields.items(): self.app.configs[name].calibration.az_offset=self.to_float(az,'AZ offset'); self.app.configs[name].calibration.el_offset=self.to_float(el,'EL offset')
            save_configs(self.app.config_path,self.app.configs); self.app.set_status('Calibration offsets saved.'); self.set_status('Calibration offsets saved.')
        except Exception as exc: self.fail(exc)
class ScanSettingsDialog(SimpleDialog):
    def __init__(self,app):
        super().__init__(app,'Scan Cal'); g=QGridLayout(); self.main.addLayout(g); s=load_scan_config(app.config_path); self.fields={}; self.antenna=QComboBox(); self.antenna.addItems(list(app.configs.keys())); self.antenna.setCurrentText(s.antenna_name); self.high_to_low=QCheckBox('AZ scan high to low'); self.high_to_low.setChecked(s.az_scan_high_to_low)
        specs=[('span_degrees','Span +/- deg',s.span_degrees),('increment_degrees','Increment deg',s.increment_degrees),('dwell_seconds','Dwell sec',s.dwell_seconds),('scan_count','Scans',s.scan_count)]
        g.addWidget(lbl('Antenna'),0,0); g.addWidget(self.antenna,0,1)
        for r,(key,label,value) in enumerate(specs,1): self.fields[key]=self.edit(value); g.addWidget(lbl(label),r,0); g.addWidget(self.fields[key],r,1)
        g.addWidget(self.high_to_low,5,0,1,2); self.status=lbl('Track a source and start B210 power before scanning.','faultTag'); self.main.addWidget(self.status)
        row=QHBoxLayout(); az=btn('AZ Scan'); el=btn('EL Scan'); stop=btn('Stop Scan'); close=btn('Close')
        az.clicked.connect(lambda:self.start_scan(Axis.AZIMUTH)); el.clicked.connect(lambda:self.start_scan(Axis.ELEVATION)); stop.clicked.connect(self.stop_scan); close.clicked.connect(self.request_close)
        for b in [az,el,stop]: row.addWidget(b)
        row.addStretch(1); row.addWidget(close); self.main.addLayout(row)
    def scan_config(self):
        return ScanConfig(self.to_float(self.fields['span_degrees'],'Span'),self.to_float(self.fields['increment_degrees'],'Increment'),self.to_float(self.fields['dwell_seconds'],'Dwell'),self.to_int(self.fields['scan_count'],'Scans'),self.antenna.currentText(),self.high_to_low.isChecked())
    def start_scan(self,axis):
        try:
            cfg=self.scan_config(); save_scan_config(self.app.config_path,cfg)
            starter=getattr(self.app,'start_calibration_scan',None)
            if callable(starter): starter(axis,cfg,self)
            else: self.set_status('Internal error: scan worker is unavailable.')
        except Exception as exc: self.set_status(str(exc))
    def stop_scan(self):
        stopper=getattr(self.app,'stop_scan',None)
        if callable(stopper): stopper()
        else: self.set_status('No PyQt scan worker is running.')
    def set_status(self,text): self.status.setText(text)
    def request_close(self):
        if getattr(self.app,'is_scan_running',lambda:False)():
            self.stop_scan(); self.set_status('Scan stop requested; close after scan cleanup finishes.'); return
        self.reject()
    def closeEvent(self,event):
        if getattr(self.app,'is_scan_running',lambda:False)():
            self.stop_scan(); self.set_status('Scan stop requested; close after scan cleanup finishes.'); event.ignore(); return
        event.accept()
class YFactorSettingsDialog(SimpleDialog):
    def __init__(self,app):
        super().__init__(app,'Y Factor'); g=QGridLayout(); self.main.addLayout(g); y=load_yfactor_config(app.config_path)
        self.hot=QComboBox(); self.hot.addItems(['Sun','Moon','Source']); self.hot.setCurrentText(y.hot_target)
        self.antenna=QComboBox(); self.antenna.addItems(list(app.configs.keys())); self.antenna.setCurrentText(y.antenna_name)
        self.cold=QComboBox(); self.cold.addItems(['Sun AZ / EL 80','Moon AZ / EL 80','Source AZ / EL 80','AZ/EL','RA/Dec']); self.cold.setCurrentText(y.cold_mode)
        self.workflow=QComboBox(); self.workflow.addItems(['Alternate H/C','Repeat H/C']); self.workflow.setCurrentText('Alternate H/C' if y.alternate_order else 'Repeat H/C')
        self.fields={}; specs=[('cold_az','Cold AZ',y.cold_az),('cold_el','Cold EL',y.cold_el),('cold_ra','Cold RA h',y.cold_ra),('cold_dec','Cold Dec',y.cold_dec),('count','Measurements',y.count),('dwell_seconds','Dwell sec',y.dwell_seconds)]
        for r,(label,w) in enumerate([('Hot target',self.hot),('Antenna',self.antenna),('Cold sky',self.cold)]): g.addWidget(lbl(label),r,0); g.addWidget(w,r,1)
        for r,(key,label,value) in enumerate(specs,3): self.fields[key]=self.edit(value); g.addWidget(lbl(label),r,0); g.addWidget(self.fields[key],r,1)
        g.addWidget(lbl('Workflow'),9,0); g.addWidget(self.workflow,9,1); self.status=lbl('Start B210 power before measuring.','faultTag'); self.main.addWidget(self.status)
        row=QHBoxLayout(); start=btn('Start'); stop=btn('Stop'); close=btn('Close'); start.clicked.connect(self.start_measurement); stop.clicked.connect(self.stop_measurement); close.clicked.connect(self.reject)
        row.addWidget(start); row.addWidget(stop); row.addStretch(1); row.addWidget(close); self.main.addLayout(row)
        self.hot.currentTextChanged.connect(self.on_hot_target_changed); self.on_hot_target_changed()
    def on_hot_target_changed(self):
        if self.cold.currentText() not in ['Sun AZ / EL 80','Moon AZ / EL 80','Source AZ / EL 80']: return
        if self.hot.currentText() == 'Sun': self.cold.setCurrentText('Sun AZ / EL 80')
        elif self.hot.currentText() == 'Moon': self.cold.setCurrentText('Moon AZ / EL 80')
        elif self.hot.currentText() == 'Source': self.cold.setCurrentText('Source AZ / EL 80')
    def yfactor_config(self):
        return YFactorConfig(self.antenna.currentText(),self.hot.currentText(),self.cold.currentText(),self.to_float(self.fields['cold_az'],'Cold AZ'),self.to_float(self.fields['cold_el'],'Cold EL'),self.to_float(self.fields['cold_ra'],'Cold RA'),self.to_float(self.fields['cold_dec'],'Cold Dec'),self.to_int(self.fields['count'],'Measurements'),self.to_float(self.fields['dwell_seconds'],'Dwell'),self.workflow.currentText()=='Alternate H/C')
    def start_measurement(self):
        try:
            cfg=self.yfactor_config(); save_yfactor_config(self.app.config_path,cfg)
            starter=getattr(self.app,'start_yfactor',None)
            if callable(starter): starter(self,cfg.antenna_name,cfg.hot_target,cfg.cold_mode,cfg.cold_az,cfg.cold_el,cfg.cold_ra,cfg.cold_dec,cfg.count,cfg.dwell_seconds,cfg.alternate_order)
            else: self.set_status('Internal error: Y Factor worker is unavailable.')
        except Exception as exc: self.set_status(str(exc))
    def stop_measurement(self):
        stopper=getattr(self.app,'stop_yfactor',None)
        if callable(stopper): stopper()
        else: self.set_status('No PyQt Y Factor worker is running.')
    def set_status(self,text): self.status.setText(text)
class PeakCalibrationDialog(SimpleDialog):
    def __init__(self,app):
        super().__init__(app,'Peak Calibration'); g=QGridLayout(); self.main.addLayout(g)
        self.source=QComboBox(); self.source.addItems(['Sun','Moon','Source']); current=(app.current_target.name if app.current_target else 'Source'); self.source.setCurrentText(current if current in ['Sun','Moon'] else 'Source')
        self.antenna=QComboBox(); self.antenna.addItems(list(app.configs.keys()))
        g.addWidget(lbl('Source'),0,0); g.addWidget(self.source,0,1); g.addWidget(lbl('Antenna'),1,0); g.addWidget(self.antenna,1,1)
        self.target_label=lbl('Target --'); self.antenna_label=lbl('Antenna --'); self.raw_label=lbl('Raw --'); self.offset_label=lbl('Offsets --')
        for r,w in enumerate([self.target_label,self.antenna_label,self.raw_label,self.offset_label],2): g.addWidget(w,r,0,1,2)
        self.status=lbl('Select source and antenna, then choose tracking, jog, or lock calibration.'); self.main.addWidget(self.status)
        axis=Panel(); ag=QGridLayout(axis); ag.addWidget(lbl('Axis Tracking'),0,0,1,3); az=btn('Track AZ Only'); el=btn('Track EL Only'); stop=btn('Stop Tracking'); az.clicked.connect(lambda:self.app.start_peak_axis_tracking(self,Axis.AZIMUTH,self.source.currentText(),self.antenna.currentText())); el.clicked.connect(lambda:self.app.start_peak_axis_tracking(self,Axis.ELEVATION,self.source.currentText(),self.antenna.currentText())); stop.clicked.connect(lambda:(self.app.stop_peak_tracking(), self.set_status('Peak tracking stopped.')))
        ag.addWidget(az,1,0); ag.addWidget(el,1,1); ag.addWidget(stop,1,2); self.main.addWidget(axis)
        jog=Panel(); jg=QGridLayout(jog); jg.addWidget(lbl('Manual Peak Jog'),0,0,1,3)
        dirs={'EL+':Direction.EL_UP.value,'AZ-':Direction.AZ_CCW.value,'AZ+':Direction.AZ_CW.value,'EL-':Direction.EL_DOWN.value}
        for text,row,col in [('EL+',1,1),('AZ-',2,0),('STOP',2,1),('AZ+',2,2),('EL-',3,1)]:
            b=btn(text)
            if text == 'STOP': b.clicked.connect(lambda:self.app.stop_antenna(self.antenna.currentText()))
            else:
                b.pressed.connect(lambda t=text:self.app.start_jog(self.antenna.currentText(),dirs[t])); b.released.connect(lambda:self.app.stop_jog(self.antenna.currentText()))
            jg.addWidget(b,row,col)
        self.main.addWidget(jog)
        locks=Panel(); lg=QGridLayout(locks); lg.addWidget(lbl('Calibration Lock'),0,0,1,2); laz=btn('LOCK AZ CAL'); lel=btn('LOCK EL CAL'); laz.clicked.connect(lambda:self.app.lock_peak_axis(self,Axis.AZIMUTH,self.source.currentText(),self.antenna.currentText())); lel.clicked.connect(lambda:self.app.lock_peak_axis(self,Axis.ELEVATION,self.source.currentText(),self.antenna.currentText())); lg.addWidget(laz,1,0); lg.addWidget(lel,1,1); self.main.addWidget(locks)
        row=QHBoxLayout(); row.addStretch(1); close=btn('Close'); close.clicked.connect(self.reject); row.addWidget(close); self.main.addLayout(row); self.source.currentTextChanged.connect(lambda _text:self.refresh_labels()); self.antenna.currentTextChanged.connect(lambda _text:self.refresh_labels()); self.timer=QTimer(self); self.timer.timeout.connect(self.refresh_labels); self.timer.start(1000); self.refresh_labels()
    def refresh_labels(self):
        try: target=self.app.yfactor_hot_target(self.source.currentText())
        except Exception: target=None
        name=self.antenna.currentText(); pos=self.app.positions.get(name); cfg=self.app.configs.get(name)
        self.target_label.setText('Target --' if not target else f'{target.name} AZ {target.azimuth:0.2f} EL {target.elevation:0.2f}')
        self.antenna_label.setText('Antenna --' if not pos else f'Antenna AZ {pos.azimuth:0.2f} EL {pos.elevation:0.2f}')
        self.raw_label.setText('Raw --' if not pos else f'Raw AZ {pos.raw_azimuth:0.2f} EL {pos.raw_elevation:0.2f}')
        self.offset_label.setText('Offsets --' if not cfg else f'Offsets AZ {cfg.calibration.az_offset:+0.2f} EL {cfg.calibration.el_offset:+0.2f}')
    def set_status(self,text): self.status.setText(text)
class EncodersDialog(SimpleDialog):
    def __init__(self,app):
        super().__init__(app,'Encoders'); self.table=QTableWidget(0,8); self.table.setHorizontalHeaderLabels(['Antenna','Axis','Type','Model','Version','Serial','Resolution','Position']); self.main.addWidget(self.table); scan=btn('Scan'); scan.clicked.connect(self.scan); self.main.addWidget(scan); close=QDialogButtonBox(QDialogButtonBox.Close); close.rejected.connect(self.reject); self.main.addWidget(close); self.resize(720,320)
    def scan(self):
        self.table.setRowCount(0)
        if not self.app.sessions: QMessageBox.warning(self,'Encoders','Connect antennas before encoder scan.'); return
        for name,session in self.app.sessions.items():
            try:
                data=session.scan_encoders()
                for axis,info in data.items():
                    r=self.table.rowCount(); self.table.insertRow(r); vals=[name,axis.value,info.encoder_type,info.model,info.version,info.serial,info.resolution,info.position]
                    for c,v in enumerate(vals): self.table.setItem(r,c,QTableWidgetItem(str(v)))
            except Exception as exc: QMessageBox.warning(self,'Encoders',f'{name}: {exc}')
PowerMeterPanel = B210Panel

class WT9App(QWidget):
    def __init__(self,config_path):
        super().__init__(); self.config_path=Path(config_path); self.configs=load_configs(self.config_path); self.site=load_site_config(self.config_path); self.power_config=load_power_config(self.config_path); self.sources=load_sources(self.config_path); self.selected_source_name=self.site.selected_source if self.site.selected_source in self.sources else next(iter(self.sources), '')
        self.event_log=EventLogger(Path('logs'),self.site.log_retention_days,self.site.log_level); self.state_store=AppStateStore(); self.sessions={}; self.positions={}; self.position_rate_state={}; self.cards={}; self.current_target=None; self.card_targets={}; self.tracking_kind=''; self.tracking_stop=threading.Event(); self.tracking_thread=None; self.service_stop=threading.Event(); self.service_active=False; self.park_stop=threading.Event(); self.park_active=False; self.fixed_target_mode=''; self.fixed_target_generation=0; self.tracking_starting=False; self.jog_stops={}; self.manual_axes={}; self.manual_pending={}; self.manual_modes={}; self.b210_stop=threading.Event(); self.b210_thread=None; self.events=queue.Queue(); self.event_history=[]; self.log_handle=None; self.log_writer=None; self.scan_stop=threading.Event(); self.scan_thread=None; self.scan_antenna_name=''; self.scan_axis=None; self.scan_offset_degrees=0.0; self.scan_offset_lock=threading.Lock(); self.scan_result_dialogs=[]; self.yfactor_stop=threading.Event(); self.yfactor_thread=None; self.yfactor_active=False; self.yfactor_hot_label=''; self.peak_stop=threading.Event(); self.peak_thread=None; self.tracking_nominal_az={}; self.tracking_az_comp_force={}; self.modeless_dialogs=[]; self.active_scan_antenna=''; self.active_scan_dialog=None; self.scan_resume_kind=''; self.last_user_activity=time.monotonic(); self.timeout_in_progress=False
        self.stellarium_feedback=None
        self.network_settings=QSettings('WT9', 'AntennaController')
        self.stellarium_host=self.network_settings.value('network/stellarium_host','127.0.0.1',type=str)
        self.stellarium_port=self.network_settings.value('network/stellarium_port',8090,type=int)
        self.feedback_bind=self.network_settings.value('network/feedback_bind','127.0.0.1',type=str)
        self.stellarium_pending=None
        self.stellarium_accepted=False
        self.stellarium_poll_busy=False
        self.stellarium_last_error=None
        self.stellarium_last_key=None
        self.stellarium_last_error=None
        self.stellarium_accepted_key=None
        self.stellarium_flash_phase=False
        self.voice=VoiceAnnunciator()
        self.voice_settings=QSettings('WT9', 'AntennaController')
        self.saved_voice_muted=self.voice_settings.value('voice/muted', True, type=bool)
        self.saved_voice_volume=max(0,min(100,self.voice_settings.value('voice/volume',70,type=int)))
        self._voice_state_versions={}
        self._voice_suppress_connect_stop=set()
        self.setWindowTitle(f'WT9 — Antenna Control & Tracking {application_version()}'); self.resize(1380,920); self.setMinimumSize(1250,880); self.build_ui(); self.style_ui(); self.set_status('Load config, connect antennas, then use guarded jogs.'); self.event_log.info('APP_START',version=APP_VERSION,config=str(config_path))
        self.stellarium_poll_timer=QTimer(self)
        self.stellarium_poll_timer.timeout.connect(self.poll_stellarium_selection)
        self.stellarium_poll_timer.start(2500)
        self.stellarium_flash_timer=QTimer(self)
        self.stellarium_flash_timer.timeout.connect(self.flash_stellarium_button)
        self.stellarium_flash_timer.start(650)
        QApplication.instance().installEventFilter(self)
        self.t_ref=QTimer(self); self.t_ref.timeout.connect(self.update_reference); self.t_ref.start(1000); self.t_evt=QTimer(self); self.t_evt.timeout.connect(self.process_events); self.t_evt.start(100); self.t_pos=QTimer(self); self.t_pos.timeout.connect(self.poll_positions); self.t_pos.start(1000)
    def build_ui(self):
        outer=QVBoxLayout(self); outer.setContentsMargins(0,0,0,0); outer.setSpacing(0)
        scroll=QScrollArea(self); scroll.setObjectName('dashboardScroll'); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        # Reserve the vertical scrollbar width even when sections are collapsed.
        # Otherwise showing/hiding a panel shifts the entire dashboard horizontally.
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        content=QWidget(); content.setObjectName('dashboardContent')
        main=QVBoxLayout(content); main.setContentsMargins(10,8,10,8); main.setSpacing(6)
        scroll.setWidget(content); outer.addWidget(scroll)
        title=lbl(f'WT9 {application_version()} — ANTENNA CONTROL & TRACKING','appTitle'); main.addWidget(title)
        self.overview=lbl('● EAST: Disconnected     ● WEST: Disconnected     |     Source: None     |     Mode: IDLE','overview')
        main.addWidget(self.overview)
        voice_row=QHBoxLayout(); voice_row.setSpacing(8)
        voice_row.addWidget(bold('VOICE ANNUNCIATION'))
        self.voice_mute=QCheckBox('Mute'); self.voice_mute.setChecked(self.saved_voice_muted)
        self.voice.set_muted(self.saved_voice_muted)
        self.voice_mute.toggled.connect(self.set_voice_muted)
        voice_row.addWidget(self.voice_mute)
        voice_row.addWidget(lbl('Volume'))
        self.voice_volume=QSlider(Qt.Horizontal); self.voice_volume.setRange(0,100); self.voice_volume.setValue(self.saved_voice_volume); self.voice_volume.setFixedWidth(145)
        self.voice.set_volume(self.saved_voice_volume)
        self.voice_volume.valueChanged.connect(self.set_voice_volume); voice_row.addWidget(self.voice_volume)
        self.voice_test=btn('Test Voice'); self.voice_test.clicked.connect(lambda:self.voice.announce('East','test')); voice_row.addWidget(self.voice_test)
        if not self.voice.ready:
            self.voice_test.setEnabled(False)
            voice_row.addWidget(lbl('Generate Australian voice pack (see README)','muted'))
        voice_row.addStretch(1); main.addLayout(voice_row)
        # Two toolbar rows avoid clipped text on smaller Ubuntu displays and HiDPI scaling.
        self.action_buttons={}
        toolbar_primary=QHBoxLayout(); toolbar_primary.setSpacing(6)
        toolbar_tools=QHBoxLayout(); toolbar_tools.setSpacing(6)
        primary_actions=[
            ('Connect','connectAction',self.connect_all,'SP_DialogApplyButton'),
            ('Disconnect','disconnectAction',self.disconnect_all,'SP_DialogCancelButton'),
            ('Limits','toolAction',self.open_limits,'SP_MessageBoxWarning'),
            ('Observer','toolAction',self.open_observer,'SP_FileDialogInfoView'),
            ('Tracking','toolAction',self.open_tracking_dialog,'SP_ComputerIcon'),
            ('Sources','toolAction',self.open_sources_dialog,'SP_DirIcon'),
            ('Stellarium','toolAction',self.open_stellarium_dialog,'SP_ComputerIcon'),
            ('Stellarium Live','toolAction',self.toggle_stellarium_feedback,'SP_ComputerIcon'),
            ('Network Config','toolAction',self.open_network_config,'SP_DriveNetIcon'),
        ]
        # Compact readout alongside the Stellarium controls; no extra toolbar row.
        self.stellarium_readout=QLabel('No data')
        self.stellarium_readout.setObjectName('stellariumReadout')
        self.stellarium_readout.setMinimumWidth(190)
        self.stellarium_readout.setMaximumWidth(260)
        self.stellarium_readout.setWordWrap(True)
        self.stellarium_readout.setToolTip('No Stellarium selection received')
        self.stellarium_comp_checkbox=QCheckBox('AZ Comp')
        self.stellarium_comp_checkbox.setObjectName('stellariumCompCheckbox')
        self.stellarium_comp_checkbox.setChecked(True)
        self.stellarium_comp_checkbox.setToolTip(
            'When checked, subtract active azimuth drive compensation from measured AZ '
            'before sending Stellarium Live feedback. No effect on antenna control.')
        self.stellarium_comp_checkbox.toggled.connect(self.refresh_stellarium_feedback_positions)
        tools_actions=[
            ('Calibration','toolAction',self.open_calibration_dialog,'SP_DialogApplyButton'),
            ('Peak Cal','toolAction',self.open_peak_calibration,'SP_ArrowUp'),
            ('Scan Cal','toolAction',self.open_scan_dialog,'SP_BrowserReload'),
            ('Y Factor','toolAction',self.open_yfactor_dialog,'SP_FileDialogDetailedView'),
            ('Encoders','toolAction',self.open_encoders_dialog,'SP_DriveHDIcon'),
            ('STOP ALL','emergencyAction',self.stop_all,'SP_MessageBoxCritical'),
        ]
        for layout,actions in ((toolbar_primary,primary_actions),(toolbar_tools,tools_actions)):
            for label,style,callback,icon_id in actions:
                button=btn(label,style); button.clicked.connect(callback)
                self.action_buttons[label]=button
                button.setIcon(self.style().standardIcon(getattr(QStyle,icon_id)))
                button.setMinimumWidth(max(90,button.fontMetrics().horizontalAdvance(label)+48))
                layout.addWidget(button)
                if label == 'Stellarium':
                    layout.addWidget(self.stellarium_readout)
                    layout.addWidget(self.stellarium_comp_checkbox)
            layout.addStretch(1); main.addLayout(layout)
        top2=QHBoxLayout(); top2.setSpacing(6)
        motion_actions=[
            ('Track Sun','trackAction',lambda:self.start_tracking('sun'),'SP_MediaPlay'),
            ('Track Moon','trackAction',lambda:self.start_tracking('moon'),'SP_MediaPlay'),
            ('Track Source','trackAction',lambda:self.start_tracking('source'),'SP_MediaPlay'),
            ('Stop Track','stopTrackAction',self.stop_tracking,'SP_MediaStop'),
            ('Service','serviceAction',self.service_all,'SP_FileDialogContentsView'),
            ('Park','parkAction',self.park_all,'SP_DialogSaveButton'),
        ]
        for label,style,callback,icon_id in motion_actions:
            button=btn(label,style); button.clicked.connect(callback)
            self.action_buttons[label]=button
            button.setIcon(self.style().standardIcon(getattr(QStyle,icon_id)))
            button.setMinimumWidth(max(92,button.fontMetrics().horizontalAdvance(label)+48))
            top2.addWidget(button)
        top2.addStretch(1); main.addLayout(top2)
        # Compact source / Sun / Moon / clock dashboard.
        # Keep existing label attributes: periodic update logic writes to them.
        header_panel=Panel()
        hg=QGridLayout(header_panel); hg.setContentsMargins(10,6,10,5)
        hg.setHorizontalSpacing(10); hg.setVerticalSpacing(4)
        def astro_group(title):
            panel=QFrame(); panel.setObjectName('astroGroup')
            layout=QGridLayout(panel); layout.setContentsMargins(8,4,8,4)
            layout.setHorizontalSpacing(5); layout.setVerticalSpacing(2)
            heading=lbl(title,'astroHeading'); layout.addWidget(heading,0,0,1,4)
            return panel,layout
        src,sg=astro_group('SELECTED SOURCE')
        self.source_name=bold('Target --',10); sg.addWidget(self.source_name,1,0,1,4)
        sg.addWidget(lbl('AZ','muted'),2,0)
        self.source_az=bold('--.--',11); sg.addWidget(self.source_az,2,1)
        sg.addWidget(lbl('EL','muted'),2,2)
        self.source_el=bold('--.--',11); sg.addWidget(self.source_el,2,3)
        sg.addWidget(lbl('HA','muted'),3,0)
        self.source_ha=lbl('--:--'); sg.addWidget(self.source_ha,3,1,1,3)
        sun_panel,sung=astro_group('SUN')
        self.sun=lbl('SUN AZ --.-- EL --.--')
        sung.addWidget(self.sun,1,0,1,4)
        moon_panel,moong=astro_group('MOON')
        self.moon=lbl('MOON AZ --.-- EL --.--')
        moong.addWidget(self.moon,1,0,1,4)
        clock,cg=astro_group('OBSERVATORY TIME')
        self.local=lbl('Local --'); self.utc=lbl('UTC --'); self.lmst=lbl('LMST --')
        cg.addWidget(self.local,1,0,1,4)
        cg.addWidget(self.utc,2,0,1,4)
        cg.addWidget(self.lmst,3,0,1,4)
        for col,(panel,stretch) in enumerate(((src,3),(sun_panel,2),(moon_panel,2),(clock,3))):
            hg.addWidget(panel,0,col)
            hg.setColumnStretch(col,stretch)
        self.timeout_label=lbl('Timeout off','muted')
        hg.addWidget(self.timeout_label,1,0,1,4)
        main.addWidget(header_panel)
        self.status=lbl('')
        for name in self.configs:
            card=AntennaCard(name); card.compass.set_observer_latitude(self.site.latitude); card.set_controller_config(self.configs[name]); card.jog_pressed.connect(self.start_jog); card.jog_released.connect(self.release_jog); card.jog_mode_changed.connect(self.change_manual_mode); card.stop_clicked.connect(self.stop_antenna); card.state_changed.connect(self.announce_antenna_state); self.cards[name]=card; main.addWidget(card)
        self.power=B210Panel(self.power_config); self.power.app=self; self.power.load_active_calibrations(self.config_path,self.power_config); self.power.start_clicked.connect(self.start_b210); self.power.stop_clicked.connect(self.stop_b210); self.power.cal_clicked.connect(self.open_b210_calibration); self.power.log_start_clicked.connect(self.start_b210_log); self.power.log_stop_clicked.connect(self.stop_b210_log)
        self.power_toggle=btn('▸ B210 SDR POWER / CALIBRATION — Show panel'); self.power_toggle.setObjectName('sectionToggle')
        self.power_toggle.clicked.connect(self.toggle_power_panel); main.addWidget(self.power_toggle)
        main.addWidget(self.power); self.power.hide()
        self.event_toggle=btn('▸ EVENT LOG / SYSTEM STATUS — Show panel')
        self.event_toggle.setObjectName('sectionToggle')
        self.event_toggle.clicked.connect(self.toggle_event_panel)
        main.addWidget(self.event_toggle)
        self.event_panel=QWidget()
        bottom=QHBoxLayout(self.event_panel); bottom.setContentsMargins(0,0,0,0); bottom.setSpacing(8)
        ev=Panel(); ev.setMinimumHeight(130); eg=QGridLayout(ev); eg.setContentsMargins(9,8,9,8)
        eg.addWidget(bold('▤ EVENT LOG'),0,0); ob=btn('Open Log'); ob.clicked.connect(self.open_log_hint); eg.addWidget(ob,0,3,Qt.AlignRight)
        self.event_view=QPlainTextEdit(); self.event_view.setReadOnly(True); self.event_view.setObjectName('eventView'); self.event_view.setMinimumHeight(82)
        eg.addWidget(self.event_view,1,0,1,4); bottom.addWidget(ev,3)
        status_panel=Panel(); sg=QVBoxLayout(status_panel); sg.setContentsMargins(12,9,12,9); sg.setSpacing(7)
        sg.addWidget(bold('● SYSTEM STATUS'))
        self.connection_summary=lbl('● Antenna controllers: Disconnected','systemIndicator')
        self.tracking_summary=lbl('● Tracking: Stopped','systemIndicator')
        self.motion_summary=lbl('● Motion: Stopped','systemIndicator')
        self.status_summary=lbl('Ready')
        for widget in (self.connection_summary,self.tracking_summary,self.motion_summary,self.status_summary):
            widget.setWordWrap(True); sg.addWidget(widget)
        sg.addStretch(1); bottom.addWidget(status_panel,2)
        main.addWidget(self.event_panel); self.event_panel.hide()
    def stellarium_button_style(self):
        button=self.action_buttons['Stellarium']
        if self.stellarium_accepted:
            button.setStyleSheet('QPushButton {background-color:#d88936; color:#171717; font-weight:bold;}')
        elif self.stellarium_pending is not None and self.stellarium_flash_phase:
            button.setStyleSheet('QPushButton {background-color:#e8bd77; color:#171717;}')
        else:
            button.setStyleSheet('')

    def flash_stellarium_button(self):
        self.stellarium_flash_phase=not self.stellarium_flash_phase
        self.stellarium_button_style()

    def poll_stellarium_selection(self):
        if not self.sessions:
            return
        if self.stellarium_poll_busy:
            return
        self.stellarium_poll_busy=True
        def worker():
            try:
                data=StellariumSourceReader.fetch_selection(self.stellarium_host,self.stellarium_port)
                source=StellariumSourceReader.parse_source(data)
                result=(source,None)
            except Exception as exc:
                result=(None,str(exc))
            self.emit(lambda payload:self.receive_stellarium_selection(*payload),result)
        self.run_thread(worker,'StellariumPoll')

    @staticmethod
    def stellarium_same_source(a,b):
        """Compare stable J2000 coordinates, ignoring harmless poll rounding."""
        return (a is not None and b is not None
                and a.name.strip().casefold()==b.name.strip().casefold()
                and abs(((a.ra_hours-b.ra_hours+12)%24)-12)<1e-5
                and abs(a.dec_degrees-b.dec_degrees)<1.5e-4)

    def receive_stellarium_selection(self,source,error):
        self.stellarium_poll_busy=False
        if not self.sessions:
            return
        if source is None:
            # A lost Stellarium connection invalidates the live selection, even
            # if that selection was previously accepted into the WT9 catalogue.
            # Do not remove the saved source or issue antenna movement commands.
            self.stellarium_pending=None
            self.stellarium_last_key=None
            self.stellarium_accepted_key=None
            self.stellarium_accepted=False
            self.stellarium_readout.setText('No data')
            self.stellarium_readout.setToolTip('No valid live Stellarium selection available')
            self.stellarium_button_style()
            if self.stellarium_feedback is not None:
                self.stellarium_feedback.close()
                self.stellarium_feedback=None
                self.action_buttons['Stellarium Live'].setText('Stellarium Live')
                self.style_stellarium_live_button()
                self.set_status('Stellarium data lost: Stellarium Live stopped.')
            elif error != self.stellarium_last_error:
                self.set_status('Stellarium: No data.')
            self.stellarium_last_error=error
            return
        self.stellarium_last_error=None
        # A repeated poll of the accepted selection is not a new notification.
        if self.stellarium_same_source(source,self.stellarium_accepted_key):
            self.stellarium_pending=self.stellarium_accepted_key
            self.stellarium_accepted=True
            self.stellarium_readout.setText(f'Accepted: {self.stellarium_pending.name}')
        else:
            if not self.stellarium_same_source(source,self.stellarium_pending):
                self.stellarium_pending=source
                self.stellarium_accepted=False
                self.set_status(f'New Stellarium source received: {source.name}. Press Stellarium to accept.')
            self.stellarium_readout.setText(
                f'{source.name}\nRA: {source.ra_hours:.5f} h\nDec: {source.dec_degrees:+.5f}°')
        self.stellarium_readout.setToolTip(
            f'{source.name}\nJ2000 RA: {source.ra_hours:.8f} h\nJ2000 Dec: {source.dec_degrees:+.8f}°')
        self.stellarium_button_style()

    def accept_stellarium_source(self,source):
        if not self.sessions:
            self.set_status('Connect an antenna before accepting a Stellarium source.')
            return False
        if self.tracking_kind or self.fixed_target_mode:
            QMessageBox.warning(self,'Tracking active','Stop tracking or fixed-position movement before changing the selected source.')
            return False
        self.sources[source.name]=source
        save_sources(self.config_path,self.sources,source.name)
        self.selected_source_name=source.name
        self.site.selected_source=source.name
        self.update_reference()
        # Keep the SELECTED SOURCE panel synchronized even while tracking is idle.
        self.apply_target(self.current_tracking_target('source'))
        self.stellarium_pending=source
        self.stellarium_last_key=(source.name,source.ra_hours,source.dec_degrees)
        self.stellarium_accepted=True
        self.stellarium_accepted_key=source
        self.stellarium_readout.setText(f'Accepted: {source.name}')
        self.stellarium_button_style()
        self.set_status(f'Stellarium source selected: {source.name}. Tracking not started.')
        self.event_log.info('STELLARIUM_SOURCE_IMPORT',source=source.name,ra_hours=source.ra_hours,dec_degrees=source.dec_degrees)
        return True

    def style_stellarium_live_button(self):
        button=self.action_buttons['Stellarium Live']
        if self.stellarium_feedback is not None:
            button.setStyleSheet('QPushButton {background-color:#d88936; color:#171717; font-weight:bold;}')
        else:
            button.setStyleSheet('')

    def toggle_stellarium_feedback(self):
        if not self.sessions:
            self.set_status('Connect an antenna before using Stellarium Live.')
            return
        if self.stellarium_feedback is not None:
            self.stellarium_feedback.close()
            self.stellarium_feedback=None
            self.action_buttons['Stellarium Live'].setText('Stellarium Live')
            self.style_stellarium_live_button()
            self.set_status('Stellarium antenna-position feedback stopped.')
            return
        if not self.stellarium_accepted:
            self.set_status('Accept a Stellarium source before enabling Stellarium Live.')
            return
        try:
            import astropy.coordinates  # Required for accurate ICRS/J2000 conversion.
            feedback=StellariumFeedback(self.site.latitude,self.site.longitude,bind_host=self.feedback_bind)
            feedback.start()
            self.stellarium_feedback=feedback
            self.action_buttons['Stellarium Live'].setText('Stop Stellarium Live')
            self.style_stellarium_live_button()
            self.refresh_stellarium_feedback_positions()
            self.set_status(f'Stellarium feedback listening on {self.feedback_bind}: East TCP 10001, West TCP 10002.')
        except Exception as exc:
            self.set_status(f'Stellarium feedback unavailable: {exc}. Install python3-astropy if missing.')

    def open_network_config(self):
        dialog=QDialog(self)
        dialog.setWindowTitle('Stellarium Network Configuration')
        layout=QVBoxLayout(dialog)
        layout.addWidget(lbl('Stellarium Remote Control server (Windows or localhost)'))
        layout.addWidget(lbl('Stellarium IPv4 address'))
        host=QLineEdit(self.stellarium_host); layout.addWidget(host)
        layout.addWidget(lbl('Stellarium HTTP port (default 8090)'))
        port=QLineEdit(str(self.stellarium_port)); layout.addWidget(port)
        layout.addWidget(lbl('WT9 feedback listener IPv4 (Ubuntu LAN IP or 127.0.0.1)'))
        bind=QLineEdit(self.feedback_bind); layout.addWidget(bind)
        layout.addWidget(lbl('Read-only telescope feedback: East TCP 10001 / West TCP 10002'))
        layout.addWidget(lbl('Restart Stellarium Live after changing the feedback listener address.'))
        buttons=QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        layout.addWidget(buttons)
        def save():
            try:
                h=host.text().strip(); b=bind.text().strip()
                ipaddress.IPv4Address(h); ipaddress.IPv4Address(b)
                p=int(port.text().strip())
                if not 1 <= p <= 65535:
                    raise ValueError('HTTP port must be between 1 and 65535')
            except ValueError as exc:
                QMessageBox.warning(dialog,'Invalid network settings',str(exc))
                return
            self.stellarium_host=h; self.stellarium_port=p; self.feedback_bind=b
            self.network_settings.setValue('network/stellarium_host',h)
            self.network_settings.setValue('network/stellarium_port',p)
            self.network_settings.setValue('network/feedback_bind',b)
            self.network_settings.sync()
            self.set_status('Stellarium network settings saved. Restart Stellarium Live for listener changes.')
            dialog.accept()
        buttons.accepted.connect(save)
        buttons.rejected.connect(dialog.reject)
        dialog.exec_()

    def open_stellarium_dialog(self):
        if not self.sessions:
            self.set_status('Connect an antenna before selecting a Stellarium source.')
            return
        if self.stellarium_pending is not None:
            try:
                self.accept_stellarium_source(self.stellarium_pending)
            except Exception as exc:
                self.set_status(f'Stellarium source acceptance failed: {exc}')
            return
        self.set_status('No new Stellarium source available to accept.')

    def toggle_power_panel(self):
        visible=not self.power.isVisible()
        self.power.setVisible(visible)
        self.power_toggle.setText('▾ B210 SDR POWER / CALIBRATION — Hide panel' if visible else '▸ B210 SDR POWER / CALIBRATION — Show panel')
    def toggle_event_panel(self):
        visible=not self.event_panel.isVisible()
        self.event_panel.setVisible(visible)
        self.event_toggle.setText('▾ EVENT LOG / SYSTEM STATUS — Hide panel' if visible else '▸ EVENT LOG / SYSTEM STATUS — Show panel')
    def style_ui(self):
        # Palette and hierarchy intentionally mirror the approved dashboard mockup.
        self.setStyleSheet("""
            QWidget{background:#edf2f8;color:#20354c;font-family:Arial,Helvetica,sans-serif;font-size:10pt}
            QScrollArea#dashboardScroll{border:0;background:#edf2f8}
            QLabel{background:transparent;border:none;color:#24384e}
            QLabel#muted{color:#6d8197}
            QLabel#bold{font-weight:700;color:#23486e}
            QLabel#appTitle{background:#183e70;color:white;font-size:15pt;font-weight:bold;padding:9px 14px;border-radius:5px}
            QLabel#overview{background:#e2ecf7;color:#234e7e;border:1px solid #b7cee5;padding:6px 12px;font-weight:bold;border-radius:5px}
            QLabel#antennaTitle{color:white;font-size:12pt;font-weight:bold}
            QFrame#antennaHeader{background:#183e70;border-radius:5px}
            QFrame#antennaHeader QLabel{color:white}
            QFrame#antennaHeader QLabel#stateStopped{background:#e5edf5;color:#3c5570;border:1px solid #adc2d6;border-radius:4px;padding:5px 10px}
            QFrame#antennaHeader QLabel#stateBusy{background:#1678d1;color:white;border:1px solid #77b9f0;border-radius:4px;padding:5px 10px}
            QFrame#antennaHeader QLabel#stateGood{background:#1f994e;color:white;border:1px solid #80c89b;border-radius:4px;padding:5px 10px}
            QFrame#antennaHeader QLabel#stateFault{background:#c92d3e;color:white;border:1px solid #e99ca3;border-radius:4px;padding:5px 10px}
            QFrame#astroGroup{background:#f5f9fd;border:1px solid #d8e4ef;border-radius:5px}
            QLabel#astroHeading{font-weight:bold;color:#38628e;font-size:9pt}
            QFrame#panel{background:#ffffff;border:1px solid #c7d8e9;border-radius:7px}
            QWidget#manualPad{background:#e6f0fc;border:1px solid #b8d0eb;border-radius:7px}
            QWidget#manualPad QLabel#bold{color:#174a80;font-weight:bold}
            QLabel#manualSummary{color:#526e8c;font-weight:bold;padding:4px}
            QLabel#manualSummary[moving="true"]{color:#168144}
            QLabel#systemIndicator{color:#7c8c9c;font-weight:bold}
            QLabel#systemIndicator[active="true"]{color:#188b47}
            QPushButton{background:#ffffff;color:#294560;border:1px solid #bfd0e1;border-radius:6px;padding:5px 11px;min-height:22px;min-width:68px;font-weight:600}
            QPushButton:hover{background:#e8f2ff;border-color:#4c95dc}
            QPushButton:pressed{background:#c7e2ff;border:1px solid #287ac7}
            QPushButton#connectAction{background:#1d9b54;color:white;border:1px solid #137a41}
            QPushButton#connectAction:hover{background:#167f44}
            QPushButton#disconnectAction{background:#435a73;color:white;border:1px solid #34465b}
            QPushButton#toolAction{background:#f7faff;color:#2d4c6d;border:1px solid #c5d5e6}
            QPushButton#trackAction{background:#208e4d;color:white;border:1px solid #16733c}
            QPushButton#stopTrackAction{background:#38495c;color:white;border:1px solid #2b3948}
            QPushButton#serviceAction{background:#6b7787;color:white;border:1px solid #556272}
            QPushButton#parkAction{background:#286ab5;color:white;border:1px solid #1d5796}
            QPushButton#emergencyAction{background:#c92939;color:white;border:2px solid #a71c2a;font-weight:bold;min-width:106px}
            QPushButton#manualStop{background:#d12d3e;color:white;border:1px solid #a51d2e;font-weight:bold}
            QPushButton#jogButton{background:#f7fbff;color:#22517e;border:1px solid #8bb6e0;font-weight:bold}
            QPushButton#jogButton[jogActive="true"]{background:#1879dc;color:white;border:1px solid #0757a7}
            QPushButton#sectionToggle{text-align:left;background:#dfeaf7;color:#205487;font-weight:bold;border-radius:5px;min-height:19px;padding:4px 10px}
            QPushButton#connectAction:pressed,QPushButton#connectAction[actionActive="true"]{background:#096b35;color:white;border:1px solid #054d25}
            QPushButton#disconnectAction:pressed{background:#182b41;color:white;border:1px solid #101d2c}
            QPushButton#toolAction:pressed{background:#166db8;color:white;border:1px solid #0b4c8a}
            QPushButton#trackAction:pressed,QPushButton#trackAction[actionActive="true"]{background:#086d36;color:white;border:1px solid #044a23}
            QPushButton#stopTrackAction:pressed{background:#172637;color:white;border:1px solid #0a1624}
            QPushButton#serviceAction:pressed,QPushButton#serviceAction[actionActive="true"]{background:#394555;color:white;border:1px solid #1d2938}
            QPushButton#parkAction:pressed,QPushButton#parkAction[actionActive="true"]{background:#104a94;color:white;border:1px solid #092f64}
            QPushButton#emergencyAction:pressed{background:#861320;color:white;border:2px solid #5c0912}
            QPushButton#modeSegment{background:#d8e6f5;color:#24517c;border:1px solid #a8c6e4;border-radius:4px;min-width:69px;padding:4px 5px}
            QPushButton#modeSegment:checked{background:#1c70c2;color:white;border:1px solid #145394}
            QLineEdit{background:white;color:#152f49;border:1px solid #c3d3e4;border-radius:5px;padding:4px 7px}
            QLabel#stateGood{background:#d4f2dc;color:#185f33;border:1px solid #8bc69c;padding:5px 8px}
            QLabel#stateBusy{background:#d5e9ff;color:#15528d;border:1px solid #8cbce8;padding:5px 8px}
            QLabel#stateStopped{background:#e8edf3;color:#50647a;border:1px solid #c2ceda;padding:5px 8px}
            QLabel#stateFault,QLabel#faultTag{background:#ffe1e3;color:#a6192c;border:1px solid #dba1a8;padding:5px 8px}
            QLabel#safe{background:#d6f3dd;color:#186334;border:1px solid #91caa0;padding:5px 8px;font-weight:bold}
            QFrame#telemetryGroup{background:#f4f8fd;border:1px solid #d7e2ee;border-radius:5px}
            QLabel#telemetryHeading{color:#274b72;font-weight:bold}
            QPlainTextEdit#eventView{background:#ffffff;color:#29435c;border:0;font-family:Arial,Helvetica,sans-serif;font-size:10pt}
        """)
    def emit(self,fn,arg=None): self.events.put((fn,arg))
    def process_events(self):
        while True:
            try: fn,arg=self.events.get_nowait()
            except queue.Empty: break
            fn(arg)
        self.refresh_system_status()
    def set_status(self,msg):
        self.status.setText(msg)
        line=f"{datetime.now().strftime('%H:%M:%S')}  {msg}"
        self.event_history.insert(0,line)
        if hasattr(self,'event_view'):
            self.event_view.setPlainText('\n'.join(self.event_history))
            self.event_view.verticalScrollBar().setValue(0)
        self.state_store.set_status(msg,SystemRunState.IDLE)
        self.refresh_system_status()
    def set_voice_muted(self,muted):
        self.voice.set_muted(muted)
        self.voice_settings.setValue('voice/muted',bool(muted))
        self.voice_settings.sync()
    def set_voice_volume(self,volume):
        self.voice.set_volume(volume)
        self.voice_settings.setValue('voice/volume',int(volume))
        self.voice_settings.sync()
    def announce_antenna_state(self,name,state):
        # The card is the authority: never speak a status that has already
        # changed on screen. Rapid CONNECTING/STOPPED/TRACKING transitions
        # should not leave stale announcements in the audio queue.
        version=self._voice_state_versions.get(name,0)+1
        self._voice_state_versions[name]=version
        if state=='STOPPED' and name in self._voice_suppress_connect_stop:
            self._voice_suppress_connect_stop.discard(name)
            return  # Successful connection is not a user STOP command.
        QTimer.singleShot(650,lambda n=name,expected=state,v=version:self._announce_stable_state(n,expected,v))
    def _announce_stable_state(self,name,state,version):
        card=self.cards.get(name)
        if (card is None or self._voice_state_versions.get(name)!=version
                or card.state.text()!=state or self.voice_mute.isChecked()):
            return
        self.voice.announce(name,state)
    def refresh_system_status(self):
        if not hasattr(self,'connection_summary'): return
        total=len(self.configs); connected=len(self.sessions)
        for name,card in self.cards.items():
            if name in self.configs:
                card.set_controller_config(self.configs[name])
        self.connection_summary.setText(f'● Antenna controllers: Connected ({connected}/{total})' if connected else '● Antenna controllers: Disconnected')
        for widget,active in ((self.connection_summary,connected>0),(self.tracking_summary,bool(self.tracking_kind)),(self.motion_summary,bool(self.manual_axes or self.park_active or self.service_active))):
            widget.setProperty('active',active)
            widget.style().unpolish(widget); widget.style().polish(widget)
        tracking='Active' if self.tracking_kind else 'Stopped'
        self.tracking_summary.setText(f'● Tracking: {tracking}')
        manual_labels={Direction.AZ_CW.value:'AZ+',Direction.AZ_CCW.value:'AZ-',Direction.EL_UP.value:'EL+',Direction.EL_DOWN.value:'EL-'}
        manual=', '.join(f'{name} {manual_labels[entry["direction"]]}' for (name,axis),entry in self.manual_axes.items())
        fixed='Parking' if self.park_active else ('Servicing' if self.service_active else '')
        self.motion_summary.setText('● Motion: '+(fixed or manual or 'Stopped'))
        self.status_summary.setText(self.status.text())
        if hasattr(self,'action_buttons'):
            active_actions={'Connect':bool(self.sessions),
                            'Track Sun':self.tracking_kind=='sun',
                            'Track Moon':self.tracking_kind=='moon',
                            'Track Source':self.tracking_kind=='source',
                            'Service':self.service_active,
                            'Park':self.park_active}
            for label,button in self.action_buttons.items():
                active=active_actions.get(label,False)
                if button.property('actionActive')!=active:
                    button.setProperty('actionActive',active)
                    button.style().unpolish(button); button.style().polish(button)
        if hasattr(self,'overview'):
            states='     '.join(f'● {name.upper()} ({serial_port_display(self.configs[name].port)}): '+('Connected' if name in self.sessions else 'Disconnected') for name in self.cards)
            mode=fixed or ('MANUAL' if manual else ('TRACKING' if self.tracking_kind else 'IDLE'))
            source=self.source_name.text() if hasattr(self,'source_name') else 'None'
            self.overview.setText(f'{states}     |     Source: {source}     |     Mode: {mode}')
    def eventFilter(self,obj,event):
        if event.type() in (QEvent.MouseButtonPress,QEvent.KeyPress):
            self.last_user_activity=time.monotonic()
            if self.timeout_in_progress and self.sessions:
                self.timeout_in_progress=False
        return super().eventFilter(obj,event)
    def update_timeout_display(self):
        if not hasattr(self,'timeout_label'): return
        if not self.site.timeout_enabled:
            self.timeout_label.setText('Timeout off'); return
        if not self.sessions:
            self.timeout_label.setText('Timeout stopped'); return
        timeout_seconds=max(60.0,float(self.site.timeout_minutes)*60.0)
        remaining=max(0.0,timeout_seconds-(time.monotonic()-self.last_user_activity))
        total=int(math.ceil(remaining)); minutes,seconds=divmod(total,60)
        action='park' if self.site.timeout_action == 'park_disconnect' else 'disconnect'
        self.timeout_label.setText(f'Timeout {minutes:02d}:{seconds:02d} to {action}')
    def check_app_timeout(self):
        if not self.site.timeout_enabled or self.timeout_in_progress or not self.sessions: return
        timeout_seconds=max(60.0,float(self.site.timeout_minutes)*60.0)
        if time.monotonic()-self.last_user_activity < timeout_seconds: return
        self.timeout_in_progress=True; action=self.site.timeout_action; self.event_log.warn('APP_TIMEOUT',action=action,timeout_minutes=self.site.timeout_minutes)
        if action == 'park_disconnect':
            self.set_status('Timeout: parking antennas.'); self.park_all()
        else:
            self.set_status('Timeout: disconnecting controllers.'); self.disconnect_all()
    def info(self,msg): QMessageBox.information(self,'WT9',msg)
    def open_log_hint(self): self.info('Event logs are in the logs directory beside the app.')
    def open_limits(self): LimitsDialog(self).exec_()
    def open_observer(self): ObserverDialog(self).exec_()
    def refresh_zenith_rise_markers(self):
        for card in self.cards.values():
            card.compass.set_observer_latitude(self.site.latitude)

    def open_tracking_dialog(self): TrackingDialog(self).exec_()
    def open_sources_dialog(self): SourcesDialog(self).exec_()
    def open_calibration_dialog(self): CalibrationDialog(self).exec_()
    def open_scan_dialog(self):
        d=ScanSettingsDialog(self); d.setWindowModality(Qt.NonModal); d.setAttribute(Qt.WA_DeleteOnClose,True); self.modeless_dialogs.append(d); d.destroyed.connect(lambda _obj,d=d: self.modeless_dialogs.remove(d) if d in self.modeless_dialogs else None); d.show()
    def open_yfactor_dialog(self):
        d=YFactorSettingsDialog(self); d.setWindowFlags(d.windowFlags()|Qt.Window); d.setWindowModality(Qt.NonModal); self.modeless_dialogs.append(d); d.destroyed.connect(lambda _obj,d=d: self.modeless_dialogs.remove(d) if d in self.modeless_dialogs else None); d.show()
    def open_encoders_dialog(self): EncodersDialog(self).exec_()
    def open_peak_calibration(self): PeakCalibrationDialog(self).exec_()
    def open_b210_calibration(self): B210CalibrationDialog(self).exec_()
    def run_thread(self,fn,name='WT9Worker'):
        thread=threading.Thread(target=fn,name=name,daemon=True); thread.start(); return thread
    def connect_all(self):
        pending=[(n,c) for n,c in self.configs.items() if n not in self.sessions]
        if not pending: self.set_status('Already connected.'); return
        self.set_status('Connecting antennas...')
        for n,_ in pending: self.cards[n].set_state('CONNECTING')
        def one(n,c):
            try:
                s=SafeAntenna(c,self.motion_event); p=s.read_position(); s.update_oled_connected(); self.emit(lambda data:self.finish_connect(*data),(n,s,p,''))
            except Exception as e: self.emit(lambda data:self.finish_connect(*data),(n,None,None,str(e)))
        for n,c in pending: self.run_thread(lambda n=n,c=c: one(n,c),f'Connect{n}')
    def finish_connect(self,name,session,pos,error):
        if session:
            self.sessions[name]=session; self.positions[name]=pos; self.cards[name].set_position(pos); self._voice_suppress_connect_stop.add(name); self.cards[name].set_state('STOPPED'); self.set_status(f'{name} connected. Connected {len(self.sessions)}/{len(self.configs)} antennas.')
        else: self.cards[name].set_state('FAULT'); self.set_status(f'{name} connect fault: {error}')
    def disconnect_all(self):
        self.reset_stellarium_connection_state()
        for name in self.cards: self.stop_jog(name)
        self.service_stop.set(); self.park_stop.set()
        self.stop_all(); sessions=list(self.sessions.items()); self.sessions.clear()
        def worker():
            for n,s in sessions:
                try: s.close()
                except Exception: pass
                self.emit(lambda name:self.finish_disconnect(name),n)
        self.run_thread(worker,'Disconnect')
    def reset_stellarium_connection_state(self):
        if self.stellarium_feedback is not None:
            self.stellarium_feedback.close()
            self.stellarium_feedback=None
        self.action_buttons['Stellarium Live'].setText('Stellarium Live')
        self.style_stellarium_live_button()
        self.stellarium_pending=None
        self.stellarium_last_key=None
        self.stellarium_accepted_key=None
        self.stellarium_accepted=False
        self.stellarium_readout.setText('No data')
        self.stellarium_readout.setToolTip('Connect an antenna to receive Stellarium selections')
        self.stellarium_button_style()

    def finish_disconnect(self,name):
        self.positions.pop(name,None); self.position_rate_state.pop(name,None); self.tracking_nominal_az.pop(name,None); self.tracking_az_comp_force.pop(name,None); self.cards[name].set_position(None); self.cards[name].set_target(None,None); self.cards[name].set_state('DISCONNECTED'); self.set_status(f'{name} disconnected.')
        if not self.sessions:
            self.reset_stellarium_connection_state()
            self.timeout_in_progress=False
    def stop_tracking(self):
        if self.park_active or self.service_active:
            self.stop_all()
            self.set_status('Stop Track: Park/Service stop requested.')
            return
        self.tracking_stop.set(); self.tracking_kind=''; self.tracking_nominal_az.clear(); self.tracking_az_comp_force.clear()
        for n in self.sessions:
            if n in self.cards: self.position_rate_state.pop(n,None); self.cards[n].set_state('STOPPED')
        self.set_status('Tracking stopped.')
    def stop_all(self):
        self.clear_fixed_target_display()
        for name in self.cards: self.stop_jog(name)
        self.service_stop.set(); self.park_stop.set(); self.tracking_stop.set(); self.tracking_nominal_az.clear(); self.tracking_az_comp_force.clear(); [ev.set() for ev in self.jog_stops.values()]
        for n,s in list(self.sessions.items()): self.run_thread(lambda s=s:s.stop_all(),f'Stop{n}'); self.position_rate_state.pop(n,None); self.cards[n].set_state('STOPPED')
        self.set_status('Stopped.')
    def stop_antenna(self,name):
        self.clear_fixed_target_display()
        self.service_stop.set(); self.park_stop.set()
        self.stop_jog(name); s=self.sessions.get(name)
        if s: self.run_thread(lambda:s.stop_all(),f'Stop{name}')
        self.position_rate_state.pop(name,None)
        self.cards[name].set_state('STOPPED')
    def manual_axis(self,direction):
        return 'AZ' if direction in (Direction.AZ_CW.value,Direction.AZ_CCW.value) else 'EL'
    def change_manual_mode(self,name,mode):
        self.manual_modes[name]=mode
        self.stop_jog(name)
        self.set_status(f'{name} manual mode: {mode}')
    def release_jog(self,name,direction):
        if self.manual_modes.get(name,'MOMENTARY')=='MOMENTARY':
            key=(name,self.manual_axis(direction))
            entry=self.manual_axes.get(key)
            if entry and entry['direction']==direction: self.stop_manual_axis(name,key[1])
            elif self.manual_pending.get(key)==direction: self.manual_pending.pop(key,None)
    def start_jog(self,name,direction_text):
        if self.service_active or self.park_active or self.tracking_kind or self.tracking_starting:
            self.set_status('Manual jog rejected: automatic motion active.'); return
        if (self.scan_thread and self.scan_thread.is_alive()) or self.yfactor_active or (self.peak_thread and self.peak_thread.is_alive()):
            self.set_status('Manual jog rejected: calibration or scan active.'); return
        session=self.sessions.get(name)
        if not session: self.set_status(f'{name} not connected.'); return
        axis=self.manual_axis(direction_text); key=(name,axis)
        current=self.manual_axes.get(key)
        latched=self.manual_modes.get(name,'MOMENTARY')=='LATCHED'
        if current:
            if current['direction']==direction_text:
                if latched: self.stop_manual_axis(name,axis)
                return
            self.manual_pending[key]=direction_text
            current['stop'].set()
            return
        self.launch_manual_axis(name,axis,direction_text,session)
    def launch_manual_axis(self,name,axis,direction_text,session):
        key=(name,axis)
        if key in self.manual_axes or self.sessions.get(name) is not session: return
        stop=threading.Event()
        entry={'direction':direction_text,'stop':stop}
        self.manual_axes[key]=entry
        self.refresh_manual_display(name)
        def update(pos): self.emit(lambda data:self.update_position(*data),(name,pos))
        def worker():
            error=None
            try: session.guarded_jog(Direction(direction_text),session.config.gui_speed,None,stop,update)
            except Exception as exc: error=str(exc)
            finally: self.emit(lambda data:self.manual_axis_finished(*data),(name,axis,entry,error))
        entry['thread']=self.run_thread(worker,f'Manual{name}{axis}')
    def manual_axis_finished(self,name,axis,entry,error):
        key=(name,axis)
        if self.manual_axes.get(key) is not entry: return
        self.manual_axes.pop(key,None)
        if error:
            self.manual_pending.pop(key,None)
            self.stop_jog(name)
            self.mark_fault(name,error)
        else:
            pending=self.manual_pending.pop(key,None)
            if pending and name in self.sessions and not self.park_active and not self.service_active and not self.tracking_kind:
                self.launch_manual_axis(name,axis,pending,self.sessions[name])
        self.refresh_manual_display(name)
        if error: self.cards[name].set_state('FAULT')
    def refresh_manual_display(self,name):
        card=self.cards.get(name)
        if not card: return
        directions=[entry['direction'] for (n,_),entry in self.manual_axes.items() if n==name]
        card.set_manual_active(directions)
        self.refresh_system_status()
        if not directions and card.state.text().startswith('MANUAL'): card.set_state('STOPPED')
        if any(self.manual_axes):
            self.source_name.setText('Manual Control')
            self.source_az.setText('--.--'); self.source_el.setText('--.--'); self.source_ha.setText('--')
        elif self.source_name.text()=='Manual Control':
            self.source_name.setText('Target --')
    def stop_manual_axis(self,name,axis):
        key=(name,axis)
        self.manual_pending.pop(key,None)
        entry=self.manual_axes.get(key)
        if entry: entry['stop'].set()
    def stop_jog(self,name):
        for axis in ('AZ','EL'): self.stop_manual_axis(name,axis)
    def start_tracking(self,kind):
        if self.service_active or self.park_active: self.set_status('Tracking rejected: fixed-position movement active.'); return
        if self.manual_axes: self.set_status('Tracking rejected: stop manual motion first.'); return
        self.clear_fixed_target_display()
        if not self.sessions: self.set_status('Connect antennas before tracking.'); return
        if self.tracking_starting:
            self.set_status('Tracking start already in progress.'); return
        if self.tracking_kind == kind and self.tracking_thread and self.tracking_thread.is_alive() and not self.tracking_stop.is_set():
            try:
                target=self.current_tracking_target(kind); self.apply_target(target); self.set_status(f'Already tracking {target.name}.')
            except Exception as e:
                self.set_status(f'Tracking fault: {e}')
            return
        self.tracking_starting=True
        try:
            if (self.tracking_thread and self.tracking_thread.is_alive()) or self.tracking_kind:
                self.tracking_stop.set()
                thread=self.tracking_thread
                if thread and thread.is_alive() and threading.current_thread() is not thread:
                    thread.join(timeout=min(10.0,max(2.0,self.site.track_interval_seconds+1.0)))
                    if thread.is_alive():
                        self.tracking_kind=''
                        self.set_status('Previous tracking is still stopping; try again in a moment.'); return
                self.tracking_kind=''
            stop=threading.Event()
            target=self.current_tracking_target(kind)
            for n,s in self.sessions.items():
                effective_target=self.apply_scan_offset(target,n)
                s.config.limits.assert_position_allowed(effective_target.azimuth,effective_target.elevation)
        except Exception as e:
            self.tracking_kind=''; self.current_target=None; self.tracking_stop=threading.Event(); self.set_status(f'Tracking fault: {e}'); return
        finally:
            self.tracking_starting=False
        self.tracking_nominal_az.clear(); self.tracking_az_comp_force.clear(); self.tracking_stop=stop; self.tracking_kind=kind; self.apply_target(target)
        for n in self.sessions: self.cards[n].set_state('TRACKING')
        self.tracking_thread=self.run_thread(lambda:self.tracking_loop(kind,stop),'Tracking'); self.set_status(f'Tracking {kind.title()}.')
    def tracking_loop(self,kind,stop):
        active_threads={}
        try:
            while not stop.is_set():
                target=self.current_tracking_target(kind); self.emit(lambda t:self.apply_target(t),target)
                for name,session in list(self.sessions.items()):
                    if stop.is_set(): break
                    thread=active_threads.get(name)
                    if thread and thread.is_alive(): continue
                    thread=threading.Thread(target=lambda n=name,s=session:self.tracking_worker(n,s,kind,stop),name=f'Track{name}',daemon=True)
                    active_threads[name]=thread; thread.start()
                until=time.monotonic()+max(0.1,self.site.track_interval_seconds)
                while not stop.is_set() and time.monotonic()<until: time.sleep(0.1)
        except Exception as e:
            if stop is self.tracking_stop:
                self.tracking_kind=''; self.tracking_stop.set()
            self.emit(lambda m:self.set_status(f'Tracking fault: {m}'),str(e))
        finally:
            for thread in list(active_threads.values()):
                if thread.is_alive(): thread.join(timeout=1.0)
            if stop is self.tracking_stop and stop.is_set() and not self.tracking_kind:
                self.emit(lambda _x:self.finish_tracking_fault_states(),None)
            if stop is self.tracking_stop and self.tracking_thread is threading.current_thread():
                self.tracking_thread=None
    def tracking_worker(self,name,session,kind,stop):
        try:
            target=self.current_tracking_target(kind)
            effective_target=self.apply_scan_offset(target,name)
            force_comp=self.az_lh_compensation_for_tracking(name,session,effective_target,'TRACKING')
            display_state=self.movement_display_state(name,session,effective_target,'TRACKING')
            self.emit(lambda data:self.cards[data[0]].set_state(data[1]),(name,display_state))
            session.config.limits.assert_position_allowed(effective_target.azimuth,effective_target.elevation)
            def live_target(_pos):
                latest=self.current_tracking_target(kind)
                self.emit(lambda t:self.apply_target(t),latest)
                effective=self.apply_scan_offset(latest,name)
                return effective.azimuth,effective.elevation
            session.guarded_slew_to(effective_target.azimuth,effective_target.elevation,session.config.az_track_speed,session.config.el_track_speed,stop,self.az_tol(),self.el_tol(),self.site.az_stop_tolerance_degrees,self.site.el_stop_tolerance_degrees,self.site.az_slow_speed,self.site.el_slow_speed,self.site.az_slow_threshold_degrees,self.site.el_slow_threshold_degrees,lambda p,n=name:self.emit(lambda data:self.update_position(*data),(n,p)),target_callback=live_target,apply_az_low_to_high_compensation=True,force_az_low_to_high_compensation=force_comp)
            if not stop.is_set():
                try:
                    latest=self.apply_scan_offset(self.current_tracking_target(kind),name)
                    state=self.movement_display_state(name,session,latest,'TRACKING')
                except Exception:
                    state='TRACKING'
                self.emit(lambda data:self.cards[data[0]].set_state(data[1]),(name,state))
        except Exception as e:
            if stop is self.tracking_stop:
                self.tracking_kind=''; self.tracking_az_comp_force.clear(); stop.set()
                for _name,_session in list(self.sessions.items()):
                    self.run_thread(lambda s=_session:s.stop_all(),f'TrackFaultStop{_name}')
            self.emit(lambda data:self.mark_motion_exception(*data),(name,str(e)))
    def finish_tracking_fault_states(self):
        for name in self.sessions:
            if name in self.cards and self.cards[name].state.text() not in ('FAULT','SLEW TIMEOUT'):
                self.cards[name].set_state('STOPPED')
    def show_fixed_target_display(self,mode,sessions):
        # Each antenna can have a different configured destination.
        self.fixed_target_generation+=1
        self.fixed_target_mode=mode
        self.source_name.setText(mode)
        self.source_az.setText('--.--')
        self.source_el.setText('--.--')
        self.source_ha.setText('--')
        for name,session in sessions:
            az=getattr(session.config,mode.lower()+'_az')
            el=getattr(session.config,mode.lower()+'_el')
            target=TargetPosition(mode,az,el)
            self.card_targets[name]=target
            self.cards[name].set_target(target,self.positions.get(name),None)
        az_values=[getattr(session.config,mode.lower()+'_az') for _,session in sessions]
        el_values=[getattr(session.config,mode.lower()+'_el') for _,session in sessions]
        self.source_az.setText(f'{az_values[0]:06.2f}' if all(abs(v-az_values[0])<0.005 for v in az_values) else 'Varies')
        self.source_el.setText(f'{el_values[0]:06.2f}' if all(abs(v-el_values[0])<0.005 for v in el_values) else 'Varies')
    def clear_fixed_target_display(self):
        if not self.fixed_target_mode:
            return
        self.fixed_target_generation+=1
        self.fixed_target_mode=''
        self.card_targets.clear()
        self.current_target=None
        self.source_name.setText('Target --')
        self.source_az.setText('--.--')
        self.source_el.setText('--.--')
        self.source_ha.setText('--')
        for name,card in self.cards.items():
            card.set_target(None,self.positions.get(name))
    def fixed_move_state(self,mode,phase,token,name):
        if token != self.fixed_target_generation:
            return
        if name not in self.cards:
            return
        if phase=='done':
            self.cards[name].set_state('PARKED' if mode=='Park' else 'SERVICE')
        elif phase=='moving':
            self.cards[name].set_state('PARKING' if mode=='Park' else 'SERVICING')
    def finish_fixed_move(self,mode,token,sessions):
        if mode=='Park':
            self.park_active=False
        else:
            self.service_active=False
        if token != self.fixed_target_generation:
            return
        for name,_session in sessions:
            if name in self.cards and self.cards[name].state.text() in ('PARKING','SERVICING'):
                self.cards[name].set_state('STOPPED')
        if any(self.cards[name].state.text() in ('FAULT','SLEW TIMEOUT','STOPPED') for name,_ in sessions if name in self.cards):
            self.clear_fixed_target_display()
    def disconnect_completed_fixed_move(self,mode,token,sessions):
        """Close controllers only after every antenna has verified its final position.

        Called from the fixed-position coordinator after all motion workers join.
        Does not call stop_all(), which would erase successful completion state.
        """
        closed=[]
        failures=[]
        for name,session in sessions:
            try:
                session.close()
                closed.append(name)
            except Exception as exc:
                failures.append((name,str(exc)))
        self.emit(lambda result:self.finish_completed_fixed_disconnect(*result),
                  (mode,token,closed,failures))

    def finish_completed_fixed_disconnect(self,mode,token,closed,failures):
        # UI and session dictionary are only modified on the Qt event thread.
        for name in closed:
            self.sessions.pop(name,None)
            self.finish_disconnect(name)
        for name,error in failures:
            if name in self.cards:
                self.cards[name].set_state('FAULT')
            self.set_status(f'{mode}: {name} controller disconnect failed: {error}')
        if not self.sessions:
            self.reset_stellarium_connection_state()
        if token == self.fixed_target_generation:
            self.clear_fixed_target_display()
        if not failures:
            self.set_status(f'{mode} verified; antenna controllers disconnected.')
        else:
            self.set_status(f'{mode} verified; {len(failures)} controller disconnect(s) failed.')

    def service_all(self):
        if self.manual_axes: self.set_status("Service rejected: stop manual motion first."); return
        if not self.sessions:
            self.set_status('Connect antennas before Service.'); return
        if self.service_active or self.park_active:
            self.set_status('Fixed-position movement already active.'); return
        if self.tracking_starting or any(self.cards[name].state.text() in ('MANUAL','PARKING') for name in self.sessions):
            self.set_status('Service rejected: another antenna movement is active.'); return
        if (self.scan_thread and self.scan_thread.is_alive()) or self.yfactor_active or (self.peak_thread and self.peak_thread.is_alive()):
            self.set_status('Service rejected: finish calibration or scan first.'); return
        sessions=list(self.sessions.items())
        try:
            for name,session in sessions:
                validate_service_target(session.config.limits,session.config.service_az,session.config.service_el,
                                        self.site.az_stop_tolerance_degrees,self.site.el_stop_tolerance_degrees)
        except Exception as exc:
            self.set_status(f'Service rejected before motion: {exc}'); return
        self.service_active=True
        self.service_stop.clear()
        self.tracking_stop.set()
        self.tracking_kind=''
        self.tracking_nominal_az.clear()
        self.tracking_az_comp_force.clear()
        stop=self.service_stop
        self.show_fixed_target_display('Service',sessions)
        token=self.fixed_target_generation
        self.set_status('Moving antennas to fixed Service positions.')
        for name,_session in sessions:
            self.cards[name].set_state('SERVICING')
        def worker():
            try:
                tracking=self.tracking_thread
                if tracking is not None and tracking.is_alive():
                    tracking.join(timeout=10.0)
                    if tracking.is_alive():
                        self.emit(lambda msg:self.set_status(msg),'Service cancelled: tracking did not stop.')
                        return
                if stop.is_set(): return
                self.emit(lambda _unused:[self.fixed_move_state('Service','moving',token,n) for n,_s in sessions],None)
                verified=set()
                def move_one(name,session):
                    try:
                        if stop.is_set(): return
                        validate_service_target(session.config.limits,session.config.service_az,session.config.service_el,
                                                self.site.az_stop_tolerance_degrees,self.site.el_stop_tolerance_degrees)
                        precision_fixed_slew(session,session.config.service_az,session.config.service_el,
                            stop,self.site,self.az_tol(),self.el_tol(),
                            lambda p,n=name:self.emit(lambda data:self.update_position(*data),(n,p)),
                            lambda **data:self.event_log.info('SERVICE_VERIFY',antenna=name,**data))
                        if not stop.is_set():
                            verified.add(name)
                            self.emit(lambda n:self.fixed_move_state('Service','done',token,n),name)
                    except PositionCancelled:
                        pass
                    except Exception as exc:
                        stop.set()
                        self.emit(lambda data:self.mark_motion_exception(*data),(name,str(exc)))
                threads=[threading.Thread(target=move_one,args=(name,session),name=f'Service{name}',daemon=True)
                         for name,session in sessions]
                for thread in threads: thread.start()
                for thread in threads: thread.join()
                if not stop.is_set() and len(verified)==len(sessions):
                    self.emit(lambda msg:self.set_status(msg),'Service positions verified; disconnecting controllers.')
                    self.disconnect_completed_fixed_move('Service',token,sessions)
                else:
                    self.emit(lambda msg:self.set_status(msg),'Service movement stopped or failed.')
            finally:
                self.emit(lambda _unused:self.finish_fixed_move('Service',token,sessions),None)
        self.run_thread(worker,'Service')

    def park_all(self):
        if self.manual_axes: self.set_status("Park rejected: stop manual motion first."); return
        if self.service_active or self.park_active:
            self.set_status('Park rejected: fixed-position movement active.'); return
        if not self.sessions:
            self.set_status('Connect antennas before parking.'); return
        if self.tracking_starting or any(self.cards[name].state.text() in ('MANUAL','SERVICING') for name in self.sessions):
            self.set_status('Park rejected: another antenna movement is active.'); return
        if (self.scan_thread and self.scan_thread.is_alive()) or self.yfactor_active or (self.peak_thread and self.peak_thread.is_alive()):
            self.set_status('Park rejected: finish calibration or scan first.'); return
        sessions=list(self.sessions.items())
        try:
            for name,session in sessions:
                validate_service_target(session.config.limits,session.config.park_az,session.config.park_el,0.15,0.15)
        except Exception as exc:
            self.set_status(f'Park rejected before motion: {exc}'); return
        self.park_active=True
        self.park_stop.clear()
        stop=self.park_stop
        self.tracking_stop.set()
        self.tracking_kind=''
        self.tracking_nominal_az.clear()
        self.tracking_az_comp_force.clear()
        self.show_fixed_target_display('Park',sessions)
        token=self.fixed_target_generation
        self.set_status('Parking antennas with final-position verification.')
        for name,_session in sessions:
            self.cards[name].set_state('PARKING')
        def worker():
            try:
                tracking=self.tracking_thread
                if tracking is not None and tracking.is_alive():
                    tracking.join(timeout=10.0)
                    if tracking.is_alive():
                        self.emit(lambda msg:self.set_status(msg),'Park cancelled: tracking did not stop.')
                        return
                if stop.is_set(): return
                self.emit(lambda _unused:[self.fixed_move_state('Park','moving',token,n) for n,_s in sessions],None)
                verified=set()
                def park_one(name,session):
                    try:
                        if stop.is_set(): return
                        precision_fixed_slew(session,session.config.park_az,session.config.park_el,
                            stop,self.site,self.az_tol(),self.el_tol(),
                            lambda p,n=name:self.emit(lambda data:self.update_position(*data),(n,p)),
                            lambda **data:self.event_log.info('PARK_VERIFY',antenna=name,**data))
                        if not stop.is_set():
                            verified.add(name)
                            self.emit(lambda n:self.fixed_move_state('Park','done',token,n),name)
                    except PositionCancelled:
                        pass
                    except Exception as exc:
                        stop.set()
                        self.emit(lambda data:self.mark_motion_exception(*data),(name,str(exc)))
                threads=[threading.Thread(target=park_one,args=(name,session),name=f'Park{name}',daemon=True)
                         for name,session in sessions]
                for thread in threads: thread.start()
                for thread in threads: thread.join()
                if not stop.is_set() and len(verified)==len(sessions):
                    self.emit(lambda msg:self.set_status(msg),'Park positions verified; disconnecting controllers.')
                    self.disconnect_completed_fixed_move('Park',token,sessions)
                else:
                    self.emit(lambda msg:self.set_status(msg),'Park stopped or failed; controllers remain connected.')
            finally:
                self.emit(lambda _unused:self.finish_fixed_move('Park',token,sessions),None)
        self.run_thread(worker,'Park')

    def slew_all_to_target(self,target,activity,stop,scan_antenna=None):
        threads=[]
        for n,s in list(self.sessions.items()):
            def worker(n=n,s=s):
                try:
                    effective_target=self.apply_scan_offset(target,n)
                    state_activity='SCAN' if activity == 'SCAN' and n == scan_antenna else ('TRACKING' if activity == 'SCAN' else activity)
                    display_state=state_activity if state_activity == 'SCAN' else self.movement_display_state(n,s,effective_target,state_activity)
                    force_comp=self.az_lh_compensation_for_tracking(n,s,effective_target,activity)
                    self.emit(lambda data:self.cards[data[0]].set_state(data[1]),(n,display_state)); s.config.limits.assert_position_allowed(effective_target.azimuth,effective_target.elevation)
                    s.guarded_slew_to(effective_target.azimuth,effective_target.elevation,s.config.az_track_speed,s.config.el_track_speed,stop,self.az_tol(),self.el_tol(),self.site.az_stop_tolerance_degrees,self.site.el_stop_tolerance_degrees,self.site.az_slow_speed,self.site.el_slow_speed,self.site.az_slow_threshold_degrees,self.site.el_slow_threshold_degrees,lambda p,n=n:self.emit(lambda data:self.update_position(*data),(n,p)),apply_az_low_to_high_compensation=(activity!='SCAN'),force_az_low_to_high_compensation=force_comp)
                    if not stop.is_set(): self.emit(lambda data:self.cards[data[0]].set_state(data[1]),(n,state_activity))
                except Exception as e: self.emit(lambda data:self.mark_motion_exception(*data),(n,str(e)))
            t=threading.Thread(target=worker,daemon=True); threads.append(t); t.start()
        for t in threads: t.join()
    def az_lh_compensation_for_tracking(self,name,session,target,activity):
        if activity == 'SCAN':
            self.tracking_az_comp_force.pop(name,None)
            return False
        if activity != 'TRACKING' or not self.tracking_kind:
            self.tracking_az_comp_force.pop(name,None)
            return None
        previous=self.tracking_nominal_az.get(name)
        self.tracking_nominal_az[name]=target.azimuth
        if previous is None:
            self.tracking_az_comp_force.pop(name,None)
            return None
        try: delta=session.config.limits.azimuth_delta_to_target(previous,target.azimuth)
        except Exception: delta=shortest_angle_delta(previous,target.azimuth)
        if abs(delta) < 1e-6:
            return self.tracking_az_comp_force.get(name)
        active=delta > 0.0
        self.tracking_az_comp_force[name]=active
        return active

    def movement_display_state(self,name,session,target,activity):
        if activity != 'TRACKING': return 'SLEWING'
        pos=self.positions.get(name)
        if not pos: return 'SLEWING'
        target,_az_comp=self.drive_target_for_display(name,target)
        if not target: return 'SLEWING'
        try: az_error=abs(session.config.limits.azimuth_delta_to_target(pos.azimuth,target.azimuth))
        except Exception: az_error=abs(shortest_angle_delta(pos.azimuth,target.azimuth))
        el_error=abs(target.elevation-pos.elevation); gross_az=max(self.site.az_slow_threshold_degrees,self.az_tol()*3.0); gross_el=max(self.site.el_slow_threshold_degrees,self.el_tol()*3.0)
        return 'SLEWING' if az_error > gross_az or el_error > gross_el else 'TRACKING'
    def az_tol(self): return abs(self.site.az_track_tolerance_degrees)
    def el_tol(self): return abs(self.site.el_track_tolerance_degrees)
    def current_tracking_target(self,kind):
        if kind=='sun': return self.target_for_kind('sun')
        if kind=='moon': return self.target_for_kind('moon')
        if not self.selected_source_name or self.selected_source_name not in self.sources: raise RuntimeError('No selected source in config.')
        src=self.sources[self.selected_source_name]; return source_position(src.name,src.ra_hours,src.dec_degrees,self.site.latitude,self.site.longitude)
    def target_for_kind(self,kind):
        if kind=='sun':
            s=sun_position(self.site.latitude,self.site.longitude); return TargetPosition('Sun',s.azimuth,s.elevation)
        m=moon_position(self.site.latitude,self.site.longitude); return TargetPosition('Moon',m.azimuth,m.elevation)
    def apply_target(self,target):
        if self.fixed_target_mode: return
        self.current_target=target; self.source_name.setText(target.name); self.source_az.setText(f'{target.azimuth:06.2f}'); self.source_el.setText(f'{target.elevation:06.2f}'); self.source_ha.setText(self.hour_angle(target.name))
        for n,c in self.cards.items():
            card_target,az_comp=self.antenna_display_target(n,target)
            c.set_target(card_target,self.positions.get(n),az_comp)
    def set_antenna_target(self,name,target):
        if target is None: self.card_targets.pop(name,None)
        else: self.card_targets[name]=target
        if name in self.cards:
            card_target,az_comp=self.antenna_display_target(name,self.current_target)
            self.cards[name].set_target(card_target,self.positions.get(name),az_comp)
    def antenna_display_target(self,name,source_target):
        override=self.card_targets.get(name)
        if override is not None: return override,None
        return self.drive_target_for_display(name,source_target)
    def drive_target_for_display(self,name,source_target):
        if source_target is None: return None,None
        session=self.sessions.get(name); pos=self.positions.get(name); cfg=self.configs.get(name)
        if not self.tracking_kind or not session or not pos or not cfg: return source_target,None
        comp=max(0.0,float(getattr(cfg,'az_low_to_high_compensation',0.0) or 0.0))
        if comp <= 0.0: return source_target,0.0
        force=self.tracking_az_comp_force.get(name)
        try:
            apply_comp=force is True or (force is None and cfg.limits.azimuth_delta_to_target(pos.azimuth,source_target.azimuth) > 0.0)
            if not apply_comp: return source_target,0.0
            az=(source_target.azimuth+comp)%360.0
            cfg.limits.assert_position_allowed(az,source_target.elevation)
            return TargetPosition(source_target.name,az,source_target.elevation),comp
        except Exception:
            return source_target,0.0
    def hour_angle(self,name):
        now=datetime.now(timezone.utc); lst=local_sidereal_time(self.site.longitude,now)/15.0
        if name=='Sun': ra=sun_equatorial(now).ra_hours
        elif name=='Moon': ra=moon_equatorial(now)[0].ra_hours
        elif name in self.sources: ra=self.sources[name].ra_hours
        else: return '--'
        return ha_text(lst-ra)
    def update_reference(self):
        self.update_timeout_display(); self.check_app_timeout()
        now=datetime.now().astimezone(); utc=now.astimezone(timezone.utc); self.local.setText(f'Local {now:%Y-%m-%d %H:%M:%S %Z}'); self.utc.setText(f'UTC {utc:%Y-%m-%d %H:%M:%S}'); self.lmst.setText(f'LMST {hms(local_sidereal_time(self.site.longitude,utc)/15.0*3600)}')
        sun=self.target_for_kind('sun'); moon=self.target_for_kind('moon'); self.sun.setText(f'SUN AZ {sun.azimuth:06.2f} EL {sun.elevation:06.2f}'); self.moon.setText(f'MOON AZ {moon.azimuth:06.2f} EL {moon.elevation:06.2f}')
        if self.tracking_kind:
            try: self.apply_target(self.reference_target_for_label(self.tracking_kind,sun,moon))
            except Exception as e: self.set_status(f'Target update fault: {e}')
        elif self.yfactor_hot_label and not self.fixed_target_mode:
            try: self.apply_target(self.reference_target_for_label(self.yfactor_hot_label,sun,moon))
            except Exception as e: self.set_status(f'Y Factor target update fault: {e}')
    def reference_target_for_label(self,label,sun,moon):
        key=(label or '').strip().lower()
        if key == 'sun': return sun
        if key == 'moon': return moon
        if key == 'source': return self.current_tracking_target('source')
        if label in self.sources:
            src=self.sources[label]; return source_position(src.name,src.ra_hours,src.dec_degrees,self.site.latitude,self.site.longitude)
        return self.yfactor_hot_target(label)
    def poll_positions(self):
        if not self.sessions:
            if self.stellarium_feedback is not None:
                self.stellarium_feedback.clear()
            return
        if self.stellarium_feedback is not None:
            for antenna in ('East','West'):
                if not any(name.lower()==antenna.lower() for name in self.sessions):
                    self.stellarium_feedback.clear(antenna)
        def worker():
            for n,s in list(self.sessions.items()):
                try: self.emit(lambda data:self.update_position(*data),(n,s.read_position()))
                except Exception as e: self.emit(lambda data:self.mark_fault(*data),(n,str(e)))
        self.run_thread(worker,'Poll')
    def stellarium_feedback_azimuth(self,name,pos):
        """Sky-pointing AZ = encoder AZ - active drive compensation."""
        az=pos.azimuth
        if not self.stellarium_comp_checkbox.isChecked():
            return az
        if not self.tracking_kind or self.current_target is None:
            return az
        _target,comp=self.antenna_display_target(name,self.current_target)
        if comp is not None and comp > 0:
            return (az-comp)%360.0
        return az

    def refresh_stellarium_feedback_positions(self,*_args):
        if self.stellarium_feedback is None:
            return
        for name,pos in list(self.positions.items()):
            antenna='East' if name.lower()=='east' else ('West' if name.lower()=='west' else None)
            if antenna is not None:
                self.stellarium_feedback.update(
                    antenna,self.stellarium_feedback_azimuth(name,pos),pos.elevation)

    def update_position(self,name,pos):
        self.update_position_rates(name,pos); self.positions[name]=pos
        if self.stellarium_feedback is not None:
            antenna='East' if name.lower()=='east' else ('West' if name.lower()=='west' else None)
            if antenna is not None:
                self.stellarium_feedback.update(antenna,self.stellarium_feedback_azimuth(name,pos),pos.elevation)
        c=self.cards[name]; c.set_position(pos); card_target,az_comp=self.antenna_display_target(name,self.current_target); c.set_target(card_target,pos,az_comp); cfg=self.configs[name]; c.set_limits_ok(cfg.limits.is_az_allowed(pos.azimuth) and cfg.limits.is_el_allowed(pos.elevation))
    def antenna_rate_active(self,name):
        card=self.cards.get(name)
        if not card: return False
        return card.state.text() in ('TRACKING','SLEWING','PARKING','SERVICING','SCAN','YFACTOR','MANUAL') or card.state.text().startswith('MANUAL ')
    def update_position_rates(self,name,pos):
        now=time.monotonic(); previous=self.position_rate_state.get(name)
        card=self.cards.get(name)
        if not card:
            self.position_rate_state[name]=(pos,now,[],[]); return
        if not self.antenna_rate_active(name):
            self.position_rate_state[name]=(pos,now,[],[])
            card.set_rates(None,None); return
        if not previous:
            self.position_rate_state[name]=(pos,now,[],[])
            card.set_rates(0.0,0.0); return
        prev_pos,prev_time,az_history,el_history=self.rate_state_parts(previous); dt=now-prev_time
        if dt < RATE_MIN_INTERVAL_SECONDS:
            return
        az_rate=abs(shortest_angle_delta(prev_pos.azimuth,pos.azimuth))/dt
        el_rate=abs(pos.elevation-prev_pos.elevation)/dt
        az_history,az_rate=self.averaged_rate(az_history,az_rate)
        el_history,el_rate=self.averaged_rate(el_history,el_rate)
        self.position_rate_state[name]=(pos,now,az_history,el_history)
        card.set_rates(az_rate,el_rate)
    def rate_state_parts(self,previous):
        if len(previous) == 2:
            prev_pos,prev_time=previous; return prev_pos,prev_time,[],[]
        prev_pos,prev_time,az_history,el_history=previous
        return prev_pos,prev_time,list(az_history),list(el_history)
    def rate_average_samples(self):
        try: return max(1,int(getattr(self.site,'rate_average_samples',3)))
        except Exception: return 3
    def averaged_rate(self,history,value):
        history=(history+[value])[-self.rate_average_samples():]
        return history,sum(history)/len(history)
    def mark_fault(self,name,error):
        self.cards[name].set_state('FAULT'); self.set_status(f'{name}: {error}'); self.event_log.error('ANTENNA_FAULT',antenna=name,error=error)
    def is_slew_timeout_error(self,error):
        text=str(error).lower()
        return 'slew timed out' in text or 'axis slew timed out' in text
    def mark_motion_exception(self,name,error):
        if self.is_slew_timeout_error(error):
            self.cards[name].set_state('SLEW TIMEOUT'); self.set_status(f'{name}: slew timeout. {error}'); self.event_log.warn('SLEW_TIMEOUT',antenna=name,error=error)
        else:
            self.mark_fault(name,error)
    def motion_event(self,event,payload): self.event_log.debug(event,payload=payload)
    def validate_scan_config(self,cfg):
        if cfg.antenna_name not in self.configs: raise RuntimeError('Select East or West antenna for the scan.')
        if cfg.antenna_name not in self.sessions: raise RuntimeError(f'{cfg.antenna_name} must be connected before scanning.')
        if not (0.1 <= cfg.span_degrees <= 30.0): raise RuntimeError('Scan span must be 0.1..30.0 degrees.')
        if not (0.01 <= cfg.increment_degrees <= cfg.span_degrees): raise RuntimeError('Scan increment must be 0.01 degrees up to the scan span.')
        if not (0.1 <= cfg.dwell_seconds <= 60.0): raise RuntimeError('Dwell must be 0.1..60.0 seconds.')
        if not (1 <= cfg.scan_count <= 20): raise RuntimeError('Scan count must be 1..20.')
    def scan_offsets(self,axis,cfg):
        vals=[]; x=cfg.span_degrees
        while x >= -cfg.span_degrees - cfg.increment_degrees*0.5:
            vals.append(round(max(x,-cfg.span_degrees),6)); x-=cfg.increment_degrees
        if axis == Axis.AZIMUTH and not cfg.az_scan_high_to_low: vals.reverse()
        return vals
    def offset_target(self,target,axis,offset):
        az=(target.azimuth+offset)%360.0 if axis == Axis.AZIMUTH else target.azimuth
        el=max(0.0,min(90.0,target.elevation+offset)) if axis == Axis.ELEVATION else target.elevation
        return TargetPosition(target.name,az,el)
    def set_scan_offset(self,antenna_name=None,axis=None,offset=0.0):
        with self.scan_offset_lock:
            self.scan_antenna_name=antenna_name or ''; self.scan_axis=axis; self.scan_offset_degrees=float(offset) if antenna_name and axis else 0.0
    def apply_scan_offset(self,target,antenna_name):
        with self.scan_offset_lock:
            scan_name=self.scan_antenna_name; axis=self.scan_axis; offset=self.scan_offset_degrees
        if antenna_name != scan_name or axis is None or offset == 0.0: return target
        return self.offset_target(target,axis,offset)
    def average_scan_rows(self,rows,offsets):
        averaged=[]
        for offset in offsets:
            matching=[row for row in rows if row.get('power_value') is not None and float(row.get('offset_degrees',0.0)) == float(offset)]
            if not matching: continue
            row=dict(matching[-1]); row['power_value']=sum(float(r['power_value']) for r in matching)/len(matching); row['power_dbfs']=sum(float(r['power_dbfs']) for r in matching)/len(matching); row['sample_count']=sum(int(r.get('sample_count',0)) for r in matching); row['scan_number']='avg'; averaged.append(row)
        return averaged
    def start_calibration_scan(self,axis,cfg,dialog):
        if self.service_active or self.park_active: self.set_status('Scan rejected: fixed-position movement active.'); return
        if self.scan_thread and self.scan_thread.is_alive(): dialog.set_status('Scan already running.'); return
        if not self.tracking_kind: dialog.set_status('Start tracking Sun, Moon, or Source before scanning.'); return
        try:
            self.validate_scan_config(cfg); meas=self.power.current_power_measurement(cfg.antenna_name)
            if meas is None: dialog.set_status(f"Start B210 power and wait for CH {self.power.power_channel_for_antenna(cfg.antenna_name)} readings before scanning."); return
            save_scan_config(self.config_path,cfg); self.scan_stop.clear(); self.scan_resume_kind=self.tracking_kind; self.tracking_stop.set(); self.active_scan_antenna=cfg.antenna_name; self.active_scan_dialog=dialog; dialog.set_status(f'{axis.value} scan starting on {cfg.antenna_name}.'); self.set_status(f'{axis.value} scan starting on {cfg.antenna_name}.')
            self.scan_thread=threading.Thread(target=lambda:self.scan_worker(axis,cfg,dialog),daemon=True); self.scan_thread.start()
        except Exception as exc: dialog.set_status(str(exc))
    def is_scan_running(self):
        return bool(self.scan_thread and self.scan_thread.is_alive())
    def stop_scan(self):
        self.scan_stop.set(); self.set_scan_offset(None); self.set_status('Scan stop requested.')
        dialog=self.active_scan_dialog
        if dialog: self.emit(lambda d:d.set_status('Scan stop requested.'),dialog)
        session=self.sessions.get(self.active_scan_antenna)
        if session: self.run_thread(lambda s=session:s.stop_all(),'StopScanAntenna')
        for name,card in self.cards.items():
            if name == self.active_scan_antenna or card.state.text().upper() == 'SCAN':
                card.set_state('STOPPED')
    def scan_preload_offset(self,axis,cfg,first_offset):
        if axis != Axis.AZIMUTH: return None
        comp=max(0.0,float(self.configs[cfg.antenna_name].az_low_to_high_compensation or 0.0))
        if comp <= 0.0: return None
        return first_offset + comp if cfg.az_scan_high_to_low else first_offset - comp
    def slew_scan_offset(self,cfg,axis,offset,status_text=None,dialog=None):
        nominal=self.current_tracking_target(self.tracking_kind)
        self.set_scan_offset(cfg.antenna_name,axis,offset)
        self.emit(lambda t:self.apply_target(t),nominal)
        if status_text and dialog: self.emit(lambda s:dialog.set_status(s),status_text)
        self.slew_all_to_target(nominal,'SCAN',self.scan_stop,scan_antenna=cfg.antenna_name)
        return nominal
    def refresh_scan_dwell_tracking(self,axis,offset,antenna):
        nominal=self.current_tracking_target(self.tracking_kind)
        self.set_scan_offset(antenna,axis,offset)
        self.emit(lambda t:self.apply_target(t),nominal)
        self.slew_all_to_target(nominal,'SCAN',self.scan_stop,scan_antenna=antenna)
        return nominal,self.offset_target(nominal,axis,offset)
    def scan_dwell_correction_interval(self,dwell):
        interval=getattr(self.site,'track_interval_seconds',1.0) or 1.0
        return max(0.1,min(float(interval),1.0,float(dwell)))

    def resume_tracking_after_scan(self,kind):
        if not kind or not self.sessions: return
        try:
            target=self.current_tracking_target(kind); self.apply_target(target)
            stop=threading.Event(); self.tracking_stop=stop; self.tracking_kind=kind; self.tracking_nominal_az.clear(); self.tracking_az_comp_force.clear()
            for name,session in self.sessions.items():
                try:
                    state=self.movement_display_state(name,session,self.apply_scan_offset(target,name),'TRACKING')
                except Exception:
                    state='TRACKING'
                self.cards[name].set_state(state)
            self.run_thread(lambda:self.tracking_loop(kind,stop),'Tracking')
        except Exception as exc:
            self.tracking_kind=''; self.set_status(f'Tracking resume fault: {exc}')

    def scan_worker(self,axis,cfg,dialog):
        rows=[]; offsets=self.scan_offsets(axis,cfg); total_points=max(1,len(offsets)*cfg.scan_count); point_no=0; scan_dir=Path(self.config_path).parent/'scan'; scan_dir.mkdir(exist_ok=True); csv_path=scan_dir/f"wt9_scan_{cfg.antenna_name.lower()}_{axis.value}_{datetime.now():%Y%m%d-%H%M%S}.csv"
        try:
            self.set_scan_offset(cfg.antenna_name,axis,offsets[0] if offsets else 0.0)
            for scan_no in range(1,cfg.scan_count+1):
                if offsets and not self.scan_stop.is_set():
                    preload=self.scan_preload_offset(axis,cfg,offsets[0])
                    if preload is not None:
                        self.slew_scan_offset(cfg,axis,preload,f'{cfg.antenna_name} {axis.value} scan {scan_no}/{cfg.scan_count} preload offset {preload:+0.2f}',dialog)
                for offset in offsets:
                    if self.scan_stop.is_set(): break
                    point_no+=1
                    nominal=self.slew_scan_offset(cfg,axis,offset,f'{cfg.antenna_name} {axis.value} scan {scan_no}/{cfg.scan_count} point {point_no}/{total_points} offset {offset:+0.2f}',dialog); target=self.offset_target(nominal,axis,offset)
                    if self.scan_stop.is_set(): break
                    rows.append(self.collect_power_point(axis,offset,cfg.dwell_seconds,nominal,target,cfg.antenna_name,scan_no))
                if self.scan_stop.is_set(): break
            averaged=self.average_scan_rows(rows,offsets)
            if rows and not self.scan_stop.is_set(): self.write_scan_csv(csv_path,rows,averaged); self.emit(lambda data:self.show_scan_result(*data),(axis,cfg.antenna_name,csv_path,averaged or rows))
            msg='Scan stopped.' if self.scan_stop.is_set() else f'Scan complete: {csv_path.name}'
            self.emit(lambda m:dialog.set_status(m),msg); self.emit(lambda m:self.set_status(m),msg)
        except Exception as exc:
            self.emit(lambda m:dialog.set_status(m),str(exc)); self.emit(lambda m:self.set_status(f'Scan fault: {m}'),str(exc))
        finally:
            resume_kind=self.scan_resume_kind; self.scan_resume_kind=''
            self.set_scan_offset(None)
            if self.active_scan_antenna == cfg.antenna_name:
                self.active_scan_antenna=''; self.active_scan_dialog=None
            if resume_kind:
                self.emit(lambda k:self.resume_tracking_after_scan(k),resume_kind)
            self.scan_stop.clear()
    def collect_power_point(self,axis,offset,dwell,nominal,target,antenna,scan_no):
        vals=[]; last_sequence=getattr(self.power,'power_sequence',0); end=time.monotonic()+dwell; next_correction=time.monotonic()+self.scan_dwell_correction_interval(dwell); live_nominal=nominal; live_target=target
        while time.monotonic()<end and not self.scan_stop.is_set():
            if time.monotonic()>=next_correction:
                live_nominal,live_target=self.refresh_scan_dwell_tracking(axis,offset,antenna)
                next_correction=time.monotonic()+self.scan_dwell_correction_interval(dwell)
                if self.scan_stop.is_set(): break
            m=self.power.current_raw_power_measurement(antenna,last_sequence)
            if m:
                vals.append(m); last_sequence=getattr(self.power,'power_sequence',last_sequence)
            time.sleep(0.02)
        if not vals: raise RuntimeError('No B210 power measurements were available.')
        pos=self.positions.get(antenna); avg_val=sum(float(v['power_value']) for v in vals)/len(vals); avg_dbfs=sum(float(v['power_dbfs']) for v in vals)/len(vals)
        return {'local_time':datetime.now().astimezone().isoformat(timespec='seconds'),'antenna':antenna,'axis':axis.value,'scan_number':scan_no,'offset_degrees':offset,'nominal_az':live_nominal.azimuth,'nominal_el':live_nominal.elevation,'target_az':live_target.azimuth,'target_el':live_target.elevation,'power_value':avg_val,'power_dbfs':avg_dbfs,'power_unit':vals[-1]['power_unit'],'power_channel':vals[-1]['power_channel'],'sample_count':len(vals),'antenna_az':None if not pos else pos.azimuth,'antenna_el':None if not pos else pos.elevation,'raw_az':None if not pos else pos.raw_azimuth,'raw_el':None if not pos else pos.raw_elevation}
    def write_scan_csv(self,path,rows,averaged=None):
        with path.open('w',newline='',encoding='utf-8') as h:
            fieldnames=list(rows[0]); w=csv.DictWriter(h,fieldnames=fieldnames); w.writeheader(); w.writerows(rows)
            if averaged:
                w.writerow({key:'' for key in fieldnames}); w.writerows(averaged)
    def show_scan_result(self,axis,antenna,path,rows):
        d=QDialog(); d.setWindowTitle(f'{antenna} {axis.value} Scan'); d.setWindowModality(Qt.NonModal); d.setAttribute(Qt.WA_DeleteOnClose,True); v=QVBoxLayout(d); v.addWidget(lbl(f'{antenna} {axis.value} scan saved to {path.name}'))
        plot=ScanPlotWidget(axis,rows,d); v.addWidget(plot)
        points=[(float(r['offset_degrees']),float(r['power_value'])) for r in rows if r.get('power_value') is not None]
        fit=plot.fit_gaussian_with_slope(points) if points else None
        if fit:
            unit=str(rows[-1].get('power_unit','dBFS')); fwhm=2.35482*fit['sigma']; v.addWidget(lbl(f"Boresight error {fit['center']:+0.3f} deg; FWHM {fwhm:0.3f} deg; peak {fit['peak']:0.2f} {unit}; RMS {fit['rms']:0.3f} dB"))
        else:
            v.addWidget(lbl('Gaussian fit unavailable'))
        close=btn('Close'); close.clicked.connect(d.close); v.addWidget(close,alignment=Qt.AlignRight); d.resize(640,440); self.scan_result_dialogs.append(d); d.destroyed.connect(lambda _obj,d=d: self.scan_result_dialogs.remove(d) if d in self.scan_result_dialogs else None); d.show()
    def yfactor_hot_target(self,label):
        if label == 'Sun': return self.target_for_kind('sun')
        if label == 'Moon': return self.target_for_kind('moon')
        return self.current_tracking_target('source')
    def yfactor_cold_target(self,mode,hot,az,el,ra,dec):
        if mode == 'Sun AZ / EL 80': s=self.target_for_kind('sun'); return TargetPosition('Cold Sky',s.azimuth,80.0)
        if mode == 'Moon AZ / EL 80': m=self.target_for_kind('moon'); return TargetPosition('Cold Sky',m.azimuth,80.0)
        if mode == 'Source AZ / EL 80': return TargetPosition('Cold Sky',hot.azimuth,80.0)
        if mode in ('AZ/EL','AZ / EL'): return TargetPosition('Cold Sky',az,el)
        return source_position('Cold Sky',ra,dec,self.site.latitude,self.site.longitude)
    def yfactor_phase_target(self,phase,label,cold_mode,cold_az,cold_el,cold_ra,cold_dec):
        hot=self.yfactor_hot_target(label)
        return hot if phase=='hot' else self.yfactor_cold_target(cold_mode,hot,cold_az,cold_el,cold_ra,cold_dec)
    def yfactor_phase_target_tuple(self,phase,label,cold_mode,cold_az,cold_el,cold_ra,cold_dec):
        target=self.yfactor_phase_target(phase,label,cold_mode,cold_az,cold_el,cold_ra,cold_dec)
        return target.azimuth,target.elevation
    def yfactor_position_error(self,session,pos,target):
        return session.config.limits.azimuth_delta_to_target(pos.azimuth,target.azimuth), target.elevation-pos.elevation
    def yfactor_dwell_correction_interval(self,dwell):
        interval=getattr(self.site,'track_interval_seconds',1.0) or 1.0
        return max(0.1,min(float(interval),1.0,float(dwell)))
    def refresh_yfactor_dwell_tracking(self,antenna,session,target,hot_target=None,card_target=None,target_func=None):
        if hot_target: self.emit(lambda t:self.apply_target(t),hot_target)
        self.emit(lambda data:self.set_antenna_target(*data),(antenna,card_target))
        target_callback=(lambda _p: (target_func().azimuth,target_func().elevation)) if target_func else None
        session.guarded_slew_to(target.azimuth,target.elevation,session.config.az_track_speed,session.config.el_track_speed,self.yfactor_stop,self.az_tol(),self.el_tol(),self.site.az_stop_tolerance_degrees,self.site.el_stop_tolerance_degrees,self.site.az_slow_speed,self.site.el_slow_speed,self.site.az_slow_threshold_degrees,self.site.el_slow_threshold_degrees,lambda p:self.emit(lambda data:self.update_position(*data),(antenna,p)),target_callback=target_callback,apply_az_low_to_high_compensation=False)
        pos=session.read_position(); self.emit(lambda data:self.update_position(*data),(antenna,pos))
        return target,pos
    def yfactor_slew_and_settle(self,session,antenna,phase,label,cold_mode,cold_az,cold_el,cold_ra,cold_dec,dialog):
        last={}
        for attempt in range(1,4):
            target=self.yfactor_phase_target(phase,label,cold_mode,cold_az,cold_el,cold_ra,cold_dec)
            session.guarded_slew_to(target.azimuth,target.elevation,session.config.az_track_speed,session.config.el_track_speed,self.yfactor_stop,self.az_tol(),self.el_tol(),self.site.az_stop_tolerance_degrees,self.site.el_stop_tolerance_degrees,self.site.az_slow_speed,self.site.el_slow_speed,self.site.az_slow_threshold_degrees,self.site.el_slow_threshold_degrees,lambda p:self.emit(lambda data:self.update_position(*data),(antenna,p)),target_callback=(lambda _p, phase=phase: self.yfactor_phase_target_tuple(phase,label,cold_mode,cold_az,cold_el,cold_ra,cold_dec)))
            pos=session.read_position(); self.emit(lambda data:self.update_position(*data),(antenna,pos))
            target=self.yfactor_phase_target(phase,label,cold_mode,cold_az,cold_el,cold_ra,cold_dec); az_err,el_err=self.yfactor_position_error(session,pos,target)
            last={'target_az':target.azimuth,'target_el':target.elevation,'antenna_az':pos.azimuth,'antenna_el':pos.elevation,'az_error':az_err,'el_error':el_err,'settle_attempts':attempt}
            if abs(az_err) <= self.az_tol() and abs(el_err) <= self.el_tol(): return last
            if self.yfactor_stop.is_set(): return last
            self.emit(lambda s:dialog.set_status(s),f'Measurement {phase}: settling antenna, AZ err {az_err:+0.2f} EL err {el_err:+0.2f}.')
        raise RuntimeError(f'Y Factor {phase} did not settle within tolerance; AZ err {last.get("az_error",0.0):+0.2f} EL err {last.get("el_error",0.0):+0.2f}.')
    def start_yfactor(self,dialog,antenna,label,cold_mode,cold_az,cold_el,cold_ra,cold_dec,count,dwell,alternate):
        if self.service_active or self.park_active: self.set_status('Y Factor rejected: fixed-position movement active.'); return
        if self.yfactor_active: dialog.set_status('Y Factor already running.'); return
        if antenna not in self.sessions: dialog.set_status('Connect and select an antenna before Y Factor measurement.'); return
        if self.power.current_power_measurement(antenna) is None: dialog.set_status(f"Start B210 power and wait for CH {self.power.power_channel_for_antenna(antenna)} readings before Y Factor measurement."); return
        self.tracking_stop.set(); self.tracking_kind=''; self.tracking_nominal_az.clear(); self.tracking_az_comp_force.clear(); self.yfactor_hot_label=label; self.apply_target(self.yfactor_hot_target(label))
        self.yfactor_active=True; self.yfactor_stop.clear(); dialog.set_status(f'Y Factor starting on {antenna}.'); self.set_status(f'Y Factor starting on {antenna}.')
        self.cards[antenna].set_state('YFACTOR')
        self.yfactor_thread=threading.Thread(target=lambda:self.yfactor_worker(dialog,antenna,label,cold_mode,cold_az,cold_el,cold_ra,cold_dec,count,dwell,alternate),daemon=True); self.yfactor_thread.start()
    def stop_yfactor(self): self.yfactor_stop.set(); self.set_status('Y Factor stop requested.')
    def yfactor_resume_kind(self,label):
        return {'Sun':'sun','Moon':'moon','Source':'source'}.get(label,'')
    def finish_yfactor_state(self,antenna,resume_kind,should_resume):
        self.yfactor_active=False; self.yfactor_thread=None; self.yfactor_stop.clear(); self.yfactor_hot_label=''
        self.set_antenna_target(antenna,None)
        if should_resume and resume_kind:
            self.start_tracking(resume_kind)
        else:
            self.cards[antenna].set_state('STOPPED')
    def yfactor_worker(self,dialog,antenna,label,cold_mode,cold_az,cold_el,cold_ra,cold_dec,count,dwell,alternate):
        rows=[]; completed=False; resume_kind=self.yfactor_resume_kind(label); out=Path(self.config_path).parent/'yfactor'; out.mkdir(exist_ok=True); path=out/f"wt9_yfactor_{antenna.lower()}_{datetime.now():%Y%m%d-%H%M%S}.csv"; session=self.sessions[antenna]
        try:
            for name,s in list(self.sessions.items()):
                if name!=antenna: s.stop_all(); self.emit(lambda n:self.cards[n].set_state('STOPPED'),name)
            self.emit(lambda data:self.cards[data[0]].set_state(data[1]),(antenna,'YFACTOR'))
            for i in range(1,count+1):
                phases=('hot','cold') if (not alternate or i%2==1) else ('cold','hot'); results={}
                for phase in phases:
                    if self.yfactor_stop.is_set(): break
                    hot=self.yfactor_hot_target(label); target=self.yfactor_phase_target(phase,label,cold_mode,cold_az,cold_el,cold_ra,cold_dec)
                    card_target=target if phase!='hot' else None
                    self.emit(lambda s:dialog.set_status(s),f'Measurement {i}/{count}: {phase}.'); self.emit(lambda t:self.apply_target(t),hot); self.emit(lambda data:self.set_antenna_target(*data),(antenna,card_target)); self.emit(lambda data:self.cards[data[0]].set_state(data[1]),(antenna,'YFACTOR'))
                    start_meta=self.yfactor_slew_and_settle(session,antenna,phase,label,cold_mode,cold_az,cold_el,cold_ra,cold_dec,dialog)
                    results[phase]=self.collect_yfactor_power(antenna,dwell,session,lambda phase=phase:self.yfactor_phase_target(phase,label,cold_mode,cold_az,cold_el,cold_ra,cold_dec),start_meta,lambda label=label:self.yfactor_hot_target(label),card_target)
                if self.yfactor_stop.is_set(): break
                ydb=results['hot']['power_value']-results['cold']['power_value']; rows.append({'local_time':datetime.now().astimezone().isoformat(timespec='seconds'),'antenna':antenna,'measurement':i,'hot_power':results['hot']['power_value'],'cold_power':results['cold']['power_value'],'power_unit':results['hot']['power_unit'],'y_factor_db':ydb,'hot_start_az_error':results['hot'].get('start_az_error'),'hot_start_el_error':results['hot'].get('start_el_error'),'hot_end_az_error':results['hot'].get('end_az_error'),'hot_end_el_error':results['hot'].get('end_el_error'),'cold_start_az_error':results['cold'].get('start_az_error'),'cold_start_el_error':results['cold'].get('start_el_error'),'cold_end_az_error':results['cold'].get('end_az_error'),'cold_end_el_error':results['cold'].get('end_el_error'),'hot_settle_attempts':results['hot'].get('settle_attempts'),'cold_settle_attempts':results['cold'].get('settle_attempts')})
            if rows:
                with path.open('w',newline='',encoding='utf-8') as h: w=csv.DictWriter(h,fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
                avg=sum(r['y_factor_db'] for r in rows)/len(rows); msg=f'Y Factor {avg:0.1f} dB, n={len(rows)}'; completed=not self.yfactor_stop.is_set()
            else: msg='Y Factor stopped.'
            self.emit(lambda m:dialog.set_status(m),msg); self.emit(lambda m:self.set_status(m),msg)
        except Exception as exc:
            self.emit(lambda m:dialog.set_status(m),str(exc)); self.emit(lambda m:self.set_status(f'Y Factor fault: {m}'),str(exc))
        finally:
            stopped=self.yfactor_stop.is_set()
            self.emit(lambda data:self.finish_yfactor_state(*data),(antenna,resume_kind,completed and not stopped))
    def collect_yfactor_power(self,antenna,dwell,session=None,target_func=None,start_meta=None,hot_target_func=None,card_target=None):
        vals=[]; end=time.monotonic()+dwell; next_correction=time.monotonic()+self.yfactor_dwell_correction_interval(dwell)
        while time.monotonic()<end and not self.yfactor_stop.is_set():
            if session and target_func and time.monotonic()>=next_correction:
                target=target_func(); hot_target=hot_target_func() if hot_target_func else None
                display_target=target if card_target is not None else None
                self.refresh_yfactor_dwell_tracking(antenna,session,target,hot_target,display_target,target_func)
                next_correction=time.monotonic()+self.yfactor_dwell_correction_interval(dwell)
                if self.yfactor_stop.is_set(): break
            m=self.power.current_power_measurement(antenna)
            if m: vals.append(m)
            time.sleep(0.1)
        if not vals: raise RuntimeError('No B210 power measurements were available.')
        result={'power_value':sum(float(v['power_value']) for v in vals)/len(vals),'power_dbfs':sum(float(v['power_dbfs']) for v in vals)/len(vals),'power_unit':vals[-1]['power_unit'],'sample_count':len(vals)}
        if start_meta: result.update({'start_target_az':start_meta.get('target_az'),'start_target_el':start_meta.get('target_el'),'start_antenna_az':start_meta.get('antenna_az'),'start_antenna_el':start_meta.get('antenna_el'),'start_az_error':start_meta.get('az_error'),'start_el_error':start_meta.get('el_error'),'settle_attempts':start_meta.get('settle_attempts')})
        if session and target_func:
            pos=session.read_position(); self.emit(lambda data:self.update_position(*data),(antenna,pos)); target=target_func(); az_err,el_err=self.yfactor_position_error(session,pos,target)
            result.update({'end_target_az':target.azimuth,'end_target_el':target.elevation,'end_antenna_az':pos.azimuth,'end_antenna_el':pos.elevation,'end_az_error':az_err,'end_el_error':el_err})
        return result
    def start_peak_axis_tracking(self,dialog,axis,label,antenna):
        if self.service_active or self.park_active: self.set_status('Peak calibration rejected: fixed-position movement active.'); return
        if self.peak_thread and self.peak_thread.is_alive(): dialog.set_status('Peak tracking already running.'); return
        if antenna not in self.sessions: dialog.set_status('Connect and select an antenna.'); return
        self.tracking_stop.set(); self.tracking_nominal_az.clear(); self.tracking_az_comp_force.clear(); self.peak_stop.clear(); self.peak_thread=threading.Thread(target=lambda:self.peak_axis_worker(dialog,axis,label,antenna),daemon=True); self.peak_thread.start(); dialog.set_status(f'Tracking {axis.value} only.')
    def stop_peak_tracking(self): self.peak_stop.set(); self.set_status('Peak tracking stop requested.')
    def peak_axis_worker(self,dialog,axis,label,antenna):
        s=self.sessions[antenna]
        try:
            while not self.peak_stop.is_set():
                target=self.yfactor_hot_target(label); pos=s.read_position(); self.emit(lambda data:self.update_position(*data),(antenna,pos))
                az=target.azimuth if axis==Axis.AZIMUTH else pos.azimuth; el=target.elevation if axis==Axis.ELEVATION else pos.elevation
                s.guarded_slew_to(az,el,s.config.az_track_speed,s.config.el_track_speed,self.peak_stop,self.az_tol(),self.el_tol(),self.site.az_stop_tolerance_degrees,self.site.el_stop_tolerance_degrees,self.site.az_slow_speed,self.site.el_slow_speed,self.site.az_slow_threshold_degrees,self.site.el_slow_threshold_degrees,lambda p:self.emit(lambda data:self.update_position(*data),(antenna,p)))
                time.sleep(max(0.1,self.site.track_interval_seconds))
        except Exception as exc: self.emit(lambda m:dialog.set_status(m),str(exc))
    def lock_peak_axis(self,dialog,axis,label,antenna):
        try:
            if antenna not in self.sessions: raise RuntimeError('Antenna is not connected.')
            target=self.yfactor_hot_target(label); actual=target.azimuth if axis==Axis.AZIMUTH else target.elevation; pos=self.sessions[antenna].calibrate_axis(axis,actual); save_configs(self.config_path,self.configs); self.emit(lambda data:self.update_position(*data),(antenna,pos)); dialog.refresh_labels(); dialog.set_status(f'{axis.value} calibration locked.')
        except Exception as exc: dialog.set_status(str(exc))
    def start_b210(self):
        if self.b210_thread and self.b210_thread.is_alive(): self.set_status('B210 already running.'); return
        try: cfg=self.power.meter_config(); self.power_config=self.power.save_config(); save_power_config(self.config_path,self.power_config); self.power.load_active_calibrations(self.config_path,self.power_config)
        except Exception as e: self.set_status(f'B210 config fault: {e}'); return
        self.b210_stop.clear(); self.power.clear_reading('SDR POWER ON UNCAL')
        def worker():
            try:
                with B210PowerMeter(cfg) as meter:
                    while not self.b210_stop.is_set(): self.emit(lambda r:self.handle_b210(r),meter.read_power())
            except Exception as e:
                if not self.b210_stop.is_set(): self.emit(lambda m:self.set_status(f'B210 fault: {m}'),str(e))
            finally: self.emit(lambda _x:self.power.clear_reading('SDR RELEASED'),None)
        self.b210_thread=threading.Thread(target=worker,daemon=True); self.b210_thread.start(); self.set_status('B210 power started.')
    def stop_b210(self): self.b210_stop.set(); self.stop_b210_log(); self.set_status('B210 release requested.')
    def handle_b210(self,r):
        self.power.set_reading(r)
        if self.log_writer: self.log_writer.writerow([r.timestamp.isoformat(),f'{r.power_a_dbfs:0.3f}',f'{r.power_b_dbfs:0.3f}',r.sample_count]); self.log_handle.flush()
    def start_b210_log(self):
        if self.log_handle: self.set_status('B210 log already running.'); return
        path=Path(f'wt9_power_{datetime.now():%Y%m%d-%H%M%S}.csv'); self.log_handle=path.open('w',newline='',encoding='utf-8'); self.log_writer=csv.writer(self.log_handle); self.log_writer.writerow(['utc_time','ch_a_dbfs','ch_b_dbfs','samples']); self.set_status(f'B210 log started: {path.name}')
    def stop_b210_log(self):
        if self.log_handle: self.log_handle.close(); self.set_status('B210 log stopped.')
        self.log_handle=None; self.log_writer=None
    def closeEvent(self,event):
        if self.stellarium_feedback is not None:
            self.stellarium_feedback.close()
            self.stellarium_feedback=None
        self.voice_settings.sync()
        self.voice.close()
        self.event_log.info('APP_STOP',version=APP_VERSION); self.stop_b210(); self.stop_all()
        for s in list(self.sessions.values()):
            try: s.close()
            except Exception: pass
        event.accept()
def parse_args():
    p=argparse.ArgumentParser(description='Launch WT9 PyQt5 antenna GUI.'); p.add_argument('--config',default='wt9_ubuntu.ini',help='Config file. Default: wt9_ubuntu.ini'); return p.parse_args()
def main():
    args=parse_args(); app=QApplication([])
    app.setApplicationName('WT9')
    app.setApplicationDisplayName('WT9')
    app.setOrganizationName('WT9')
    app.setDesktopFileName('wt9')
    w=WT9App(args.config); w.show(); return app.exec_()
if __name__=='__main__': raise SystemExit(main())
