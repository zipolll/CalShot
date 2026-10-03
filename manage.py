"""Build or verify CalShot using only Python's standard library."""
import argparse
import runpy
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT / "src"))

from calshot.shortcut import DEFAULT_CALENDAR_SLOTS, write, write_editor_check, write_reminder_check, write_calendar_selection_check, write_performance_check


def verify(calendar_slots=DEFAULT_CALENDAR_SLOTS):
    runpy.run_path(str(PROJECT / "tests/verify_workflow.py"), run_name="__main__",
                  init_globals={"CALSHOT_CALENDAR_SLOTS": calendar_slots})


def positive_integer(value):
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("calendar slots must be a positive integer")
    if result < 1:
        raise argparse.ArgumentTypeError("calendar slots must be a positive integer")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "verify", "editor-check", "reminder-check", "calendar-selection-check", "performance-check"))
    parser.add_argument("--calendar-slots", type=positive_integer, default=None,
                        help=f"calendar choices shown at import (default {DEFAULT_CALENDAR_SLOTS}; build/verify only)")
    args = parser.parse_args()
    if args.calendar_slots is not None and args.command not in ("build", "verify"):
        parser.error("--calendar-slots is only available for build and verify")
    calendar_slots = DEFAULT_CALENDAR_SLOTS if args.calendar_slots is None else args.calendar_slots
    commands = {"build": lambda: write(calendar_slots), "verify": lambda: verify(calendar_slots), "editor-check": write_editor_check,
                "reminder-check": write_reminder_check, "calendar-selection-check": write_calendar_selection_check,
                "performance-check": write_performance_check}
    commands[args.command]()


if __name__ == "__main__":
    main()
