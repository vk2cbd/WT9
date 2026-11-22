"""GUI regression tests for fixed Park/Service display and Stop Track."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
from PyQt5.QtWidgets import QApplication
from wt9_ubuntu_gui import WT9App

class FixedPositionGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt=QApplication.instance() or QApplication([])
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        p=Path(self.tmp.name)/"wt9.ini"
        p.write_text("[site]\nlatitude=-32.724\nlongitude=152.13\n[antenna:East]\nport=loop://\n[antenna:West]\nport=loop://\n")
        self.app=WT9App(p)
    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()
    def setup_targets(self):
        self.app.sessions={
            "East":SimpleNamespace(config=SimpleNamespace(park_az=60.0,park_el=80.0,service_az=0.0,service_el=5.0)),
            "West":SimpleNamespace(config=SimpleNamespace(park_az=61.0,park_el=80.0,service_az=2.0,service_el=5.0)),
        }
        self.app.positions={
            "East":SimpleNamespace(azimuth=50.0,elevation=70.0),
            "West":SimpleNamespace(azimuth=50.0,elevation=70.0),
        }
    def test_park_targets_and_errors_for_each_antenna(self):
        self.setup_targets()
        self.app.show_fixed_target_display("Park",list(self.app.sessions.items()))
        self.assertEqual(self.app.source_name.text(),"Park")
        self.assertEqual(self.app.source_az.text(),"Varies")
        self.assertEqual(self.app.cards["East"].target.text(),"060.00 / 80.00")
        self.assertEqual(self.app.cards["West"].target.text(),"061.00 / 80.00")
        self.assertEqual(self.app.cards["East"].az_err.text(),"+10.00")
        self.assertEqual(self.app.cards["West"].az_err.text(),"+11.00")
        self.app.update_position("East",SimpleNamespace(azimuth=59.9,elevation=79.8))
        self.assertEqual(self.app.cards["East"].az_err.text(),"+0.10")
        self.assertEqual(self.app.cards["East"].el_err.text(),"+0.20")
    def test_service_targets_and_errors(self):
        self.setup_targets()
        self.app.show_fixed_target_display("Service",list(self.app.sessions.items()))
        self.assertEqual(self.app.source_name.text(),"Service")
        self.assertEqual(self.app.cards["East"].target.text(),"000.00 / 05.00")
        self.assertEqual(self.app.cards["West"].target.text(),"002.00 / 05.00")
    def test_stop_track_interrupts_park_and_service(self):
        for mode in ("Park","Service"):
            with self.subTest(mode=mode):
                self.setup_targets()
                self.app.show_fixed_target_display(mode,list(self.app.sessions.items()))
                self.app.park_active=(mode=="Park")
                self.app.service_active=(mode=="Service")
                calls=[]
                self.app.run_thread=lambda fn,name="":calls.append((fn,name))
                self.app.stop_tracking()
                self.assertTrue(self.app.park_stop.is_set())
                self.assertTrue(self.app.service_stop.is_set())
                self.assertEqual(len(calls),2)
                self.assertEqual(self.app.source_name.text(),"Target --")
                self.assertEqual(self.app.cards["East"].state.text(),"STOPPED")
                self.app.park_active=False
                self.app.service_active=False
    def test_stale_state_event_does_not_restore_parking_after_stop(self):
        self.setup_targets()
        self.app.show_fixed_target_display("Park",list(self.app.sessions.items()))
        token=self.app.fixed_target_generation
        self.app.clear_fixed_target_display()
        self.app.fixed_move_state("Park","moving",token,"East")
        self.assertNotEqual(self.app.cards["East"].state.text(),"PARKING")

if __name__=="__main__":
    unittest.main()
