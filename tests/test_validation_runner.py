"""Validation selection must never silently weaken full acceptance."""
import io
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from run_checks import failure_excerpt, replay_test, run_units, select_tests


class ValidationRunnerTests(unittest.TestCase):
    def fixture(self):
        class Example(unittest.TestCase):
            def test_fast(self):
                pass

            @replay_test
            def test_simulation(self):
                pass

        return unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromTestCase(Example)])

    def test_full_and_standard_discovery_keep_simulations(self):
        self.assertEqual(self.fixture().countTestCases(), 2)
        suite, omitted = select_tests(self.fixture())
        self.assertEqual(suite.countTestCases(), 2)
        self.assertEqual(omitted, 0)
        self.assertTrue(run_units(suite, io.StringIO()).wasSuccessful())

    def test_quick_only_omits_explicitly_marked_simulations(self):
        suite, omitted = select_tests(self.fixture(), quick=True)
        self.assertEqual(suite.countTestCases(), 1)
        self.assertEqual(omitted, 1)
        self.assertTrue(run_units(suite, io.StringIO()).wasSuccessful())

    def test_failure_and_output_are_preserved_in_log(self):
        class Broken(unittest.TestCase):
            def test_failure(self):
                print("diagnostic marker")
                self.fail("regression marker")

        log = io.StringIO()
        result = run_units(unittest.defaultTestLoader.loadTestsFromTestCase(Broken), log)
        self.assertFalse(result.wasSuccessful())
        self.assertIn("diagnostic marker", log.getvalue())
        self.assertIn("regression marker", log.getvalue())

    def test_failed_test_import_is_not_filtered_out(self):
        suite = unittest.TestLoader().loadTestsFromName("missing_validation_fixture_xyz")
        selected, omitted = select_tests(suite, quick=True)
        self.assertEqual(omitted, 0)
        self.assertEqual(selected.countTestCases(), 1)
        self.assertFalse(run_units(selected, io.StringIO()).wasSuccessful())

    def test_failure_excerpt_is_bounded_without_truncating_log(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "full.log"
            content = ("x" * 200 + "\n") * 100 + "failure marker\n"
            path.write_text(content)
            excerpt = failure_excerpt(path)
            self.assertLessEqual(len(excerpt), 6000)
            self.assertTrue(excerpt.endswith("failure marker\n"))
            self.assertEqual(path.read_text(), content)
