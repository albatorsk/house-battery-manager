"""Unit tests for the pure control logic (no Home Assistant needed).

Run with: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest

# Load controller.py directly so the package __init__ (which needs Home
# Assistant) is not imported.
_PATH = Path(__file__).parents[1] / "custom_components/house_battery_manager/controller.py"
_spec = importlib.util.spec_from_file_location("hbm_controller", _PATH)
controller = importlib.util.module_from_spec(_spec)
sys.modules["hbm_controller"] = controller
_spec.loader.exec_module(controller)

Measurements = controller.Measurements
Mode = controller.Mode
Settings = controller.Settings
compute = controller.compute

V = 230.0
LIMIT_A = 19.0  # 20 A fuse - 1 A margin


def phases(*amps: float) -> tuple[float, float, float]:
    return tuple(amps)  # type: ignore[return-value]


class AutoModeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.s = Settings(gain=1.0, deadband=30)

    def test_import_is_covered_by_discharge(self) -> None:
        d = compute(Measurements(grid_power=1500, battery_power=0), self.s, 0)
        self.assertEqual(d.setpoint, 1500)

    def test_export_is_absorbed_by_charging(self) -> None:
        d = compute(Measurements(grid_power=-2000, battery_power=-500), self.s, -500)
        self.assertEqual(d.setpoint, -2500)

    def test_gain_scales_correction(self) -> None:
        self.s.gain = 0.5
        d = compute(Measurements(grid_power=1000, battery_power=0), self.s, 0)
        self.assertEqual(d.setpoint, 500)

    def test_deadband_holds_previous_setpoint(self) -> None:
        d = compute(Measurements(grid_power=20, battery_power=800), self.s, 800)
        self.assertEqual(d.setpoint, 800)

    def test_uses_last_setpoint_without_battery_sensor(self) -> None:
        d = compute(Measurements(grid_power=300), self.s, 1000)
        self.assertEqual(d.setpoint, 1300)

    def test_grid_target_offset(self) -> None:
        self.s.grid_target = 500
        d = compute(Measurements(grid_power=1500, battery_power=0), self.s, 0)
        self.assertEqual(d.setpoint, 1000)

    def test_respects_max_discharge(self) -> None:
        self.s.max_discharge_power = 3000
        d = compute(Measurements(grid_power=8000, battery_power=0), self.s, 0)
        self.assertEqual(d.setpoint, 3000)

    def test_reserve_soc_blocks_discharge_but_allows_charge(self) -> None:
        d = compute(Measurements(grid_power=1000, battery_power=0, soc=10), self.s, 0)
        self.assertEqual(d.setpoint, 0)
        d = compute(Measurements(grid_power=-1000, battery_power=0, soc=10), self.s, 0)
        self.assertEqual(d.setpoint, -1000)

    def test_ev_excluded_when_battery_may_not_power_ev(self) -> None:
        self.s.battery_powers_ev = False
        d = compute(
            Measurements(grid_power=5000, battery_power=0, ev_power=4000), self.s, 0
        )
        self.assertEqual(d.setpoint, 1000)


class ForceModeTest(unittest.TestCase):
    def test_force_charge(self) -> None:
        s = Settings(mode=Mode.FORCE_CHARGE, force_charge_power=3000)
        d = compute(Measurements(grid_power=500, battery_power=0, soc=50), s, 0)
        self.assertEqual(d.setpoint, -3000)
        self.assertFalse(d.force_finished)

    def test_force_charge_finishes_at_target(self) -> None:
        s = Settings(mode=Mode.FORCE_CHARGE, charge_target_soc=80)
        d = compute(Measurements(grid_power=0, battery_power=-3000, soc=80), s, -3000)
        self.assertEqual(d.setpoint, 0)
        self.assertTrue(d.force_finished)

    def test_force_discharge_finishes_at_target(self) -> None:
        s = Settings(mode=Mode.FORCE_DISCHARGE, discharge_target_soc=20)
        d = compute(Measurements(grid_power=0, battery_power=3000, soc=19), s, 3000)
        self.assertEqual(d.setpoint, 0)
        self.assertTrue(d.force_finished)

    def test_idle_and_off(self) -> None:
        d = compute(Measurements(grid_power=3000), Settings(mode=Mode.IDLE), 2000)
        self.assertEqual(d.setpoint, 0)
        d = compute(Measurements(grid_power=3000), Settings(mode=Mode.OFF), 2000)
        self.assertIsNone(d.setpoint)


class FuseProtectionTest(unittest.TestCase):
    def test_force_charge_limited_by_most_loaded_phase(self) -> None:
        s = Settings(mode=Mode.FORCE_CHARGE, force_charge_power=5000)
        # L1 at 15 A import: 4 A headroom on that phase -> 3 * 230 * 4 = 2760 W.
        d = compute(
            Measurements(grid_power=6000, battery_power=0, phase_currents=phases(15, 5, 6)),
            s,
            0,
        )
        self.assertEqual(d.setpoint, -2760)
        self.assertTrue(d.fuse_limited)
        self.assertAlmostEqual(d.phase_headroom, 4.0)

    def test_overload_forces_discharge_even_below_reserve(self) -> None:
        s = Settings(mode=Mode.FORCE_CHARGE, force_charge_power=3000)
        # Battery charging 3000 W, L2 at 22 A: must move to +3 A per phase -> -3000 + 2070.
        d = compute(
            Measurements(
                grid_power=12000, battery_power=-3000, phase_currents=phases(18, 22, 12), soc=5
            ),
            s,
            -3000,
        )
        self.assertEqual(d.setpoint, -3000 + round(3 * V * 3))
        self.assertTrue(d.fuse_limited)

    def test_auto_discharges_to_relieve_overload(self) -> None:
        s = Settings(gain=0.1, max_discharge_power=10000)
        # Gain is low, but the fuse needs 3 A per phase on L1 -> at least 2070 W.
        d = compute(
            Measurements(grid_power=11000, battery_power=0, phase_currents=phases(22, 15, 10)),
            s,
            0,
        )
        self.assertEqual(d.setpoint, round(3 * V * 3))

    def test_export_limited_by_fuse(self) -> None:
        s = Settings(mode=Mode.FORCE_DISCHARGE, force_discharge_power=10000,
                     max_discharge_power=10000)
        # PV export of 12 A per phase; only 7 A per phase left for discharging.
        d = compute(
            Measurements(grid_power=-3 * V * 12, battery_power=0,
                         phase_currents=phases(-12, -12, -12)),
            s,
            0,
        )
        self.assertEqual(d.setpoint, round(3 * V * 7))

    def test_emergency_only_above_fuse_rating(self) -> None:
        s = Settings(gain=0.1, max_discharge_power=10000)
        # 19.5 A: above the 19 A regulation limit but below the 20 A fuse.
        d = compute(Measurements(grid_power=2000, battery_power=0,
                                 phase_currents=phases(19.5, 10, 5)), s, 0)
        self.assertTrue(d.fuse_limited)
        self.assertFalse(d.emergency)
        d = compute(Measurements(grid_power=9000, battery_power=0,
                                 phase_currents=phases(21, 10, 5)), s, 0)
        self.assertTrue(d.emergency)
        self.assertIn("fuse overloaded", d.status)

    def test_no_emergency_when_battery_already_relieves(self) -> None:
        # Over the rating, but the auto setpoint already discharges enough.
        s = Settings(gain=1.0, max_discharge_power=10000)
        d = compute(Measurements(grid_power=9000, battery_power=0,
                                 phase_currents=phases(21, 10, 5)), s, 0)
        self.assertFalse(d.fuse_limited)
        self.assertFalse(d.emergency)

    def test_total_power_used_when_no_phase_data(self) -> None:
        s = Settings(mode=Mode.FORCE_CHARGE, force_charge_power=5000)
        # 3 * 230 * 15 A = 10350 W balanced import -> 4 A headroom per phase.
        d = compute(Measurements(grid_power=10350, battery_power=0), s, 0)
        self.assertEqual(d.setpoint, -2760)

    def test_missing_grid_data_is_fail_safe(self) -> None:
        s = Settings(mode=Mode.FORCE_CHARGE)
        d = compute(Measurements(grid_power=None), s, -3000)
        self.assertEqual(d.setpoint, 0)

    def test_fuse_never_exceeded_in_steady_state(self) -> None:
        """Simulate the loop against a simple house model and check the fuse."""
        s = Settings(mode=Mode.FORCE_CHARGE, force_charge_power=10000,
                     max_charge_power=10000, gain=0.7)
        load = (12.0, 17.0, 8.0)  # per phase house load in A, single phase EV on L2
        battery = 0.0
        for _ in range(20):
            currents = tuple(i - battery / (3 * V) for i in load)
            grid = sum(currents) * V
            d = compute(Measurements(grid_power=grid, battery_power=battery,
                                     phase_currents=currents), s, battery)
            battery = d.setpoint
            new_currents = [i - battery / (3 * V) for i in load]
            self.assertLessEqual(max(abs(c) for c in new_currents), LIMIT_A + 0.01)



class BatteryFaultTest(unittest.TestCase):
    def test_fault_sets_zero_in_every_controlling_mode(self) -> None:
        for mode in (Mode.AUTO, Mode.FORCE_CHARGE, Mode.FORCE_DISCHARGE, Mode.IDLE):
            s = Settings(mode=mode)
            d = compute(
                Measurements(grid_power=3000, battery_fault="over temperature"), s, 2000
            )
            self.assertEqual(d.setpoint, 0.0)
            self.assertIn("over temperature", d.status)
            self.assertFalse(d.force_finished)

    def test_off_mode_still_sends_nothing(self) -> None:
        s = Settings(mode=Mode.OFF)
        d = compute(Measurements(grid_power=3000, battery_fault="ground fault"), s, 0)
        self.assertIsNone(d.setpoint)


if __name__ == "__main__":
    unittest.main()
