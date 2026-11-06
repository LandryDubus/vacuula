"""Back up D7 settings; optionally restore only its schedule through Vacuula."""
import argparse
import collections
import datetime
import json
import os
from pathlib import Path
import time
import urllib.parse
import urllib.request


def writable_events(events):
    result = []
    for event in events:
        writable = {key: event[key] for key in ("day", "startTime", "mode")}
        for key in ("mapId", "boundaryId"):
            if event.get(key) is not None:
                writable[key] = event[key]
        if "boundaryId" not in writable and event.get("boundary"):
            writable["boundaryId"] = event["boundary"]["id"]
        result.append(writable)
    return result


def event_counts(events):
    return collections.Counter(json.dumps(event, sort_keys=True) for event in writable_events(events))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("backup", "restore-schedule"))
    parser.add_argument("--url", required=True, help="Existing Vacuula URL, e.g. http://127.0.0.1:8050")
    parser.add_argument("--serial", required=True)
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--confirm-restore", help="For restore-schedule, repeat the robot serial")
    args = parser.parse_args()
    base = args.url.rstrip("/") + "/api"
    serial = urllib.parse.quote(args.serial, safe="")

    def request(path, data=None):
        req = urllib.request.Request(base + path, data=json.dumps(data).encode() if data is not None else None,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=40) as response:
            return json.load(response)

    def command(name, params=None):
        payload = {"cmd": name}
        if params is not None:
            payload["params"] = params
        response = request(f"/robot/{serial}/command", payload)
        if response.get("result") != "ok":
            raise RuntimeError(f"Robot rejected {name}: {response}")
        return response

    robots = request("/robots")
    robot = robots.get(args.serial, {})
    if robot.get("type") != "neato.gen3" or not robot.get("connected"):
        raise RuntimeError("The specified Gen3 robot must be connected to this server")

    if args.action == "backup":
        if args.file.exists():
            raise RuntimeError("Refusing to overwrite an existing snapshot")
        schedule = command("getSchedule")
        if not isinstance(schedule["data"]["enabled"], bool):
            raise RuntimeError("Schedule has no boolean enabled state")
        writable_events(schedule["data"]["events"])
        maps = request(f"/robot/{serial}/maps")
        snapshot = {
            "serial": args.serial,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "schedule": schedule,
            "preferences": command("getPreferences"),
            "maps": maps,
            "boundaries": {item["id"]: command("getMapBoundaries", {"mapId": item["id"]}) for item in maps},
        }
        descriptor = os.open(args.file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(snapshot, output, indent=2)
        print(f"Read-only robot snapshot saved to {args.file}")
        return

    if args.confirm_restore != args.serial:
        raise RuntimeError("Repeat the serial with --confirm-restore to authorize a schedule write")
    snapshot = json.loads(args.file.read_text(encoding="utf-8"))
    if snapshot["serial"] != args.serial:
        raise RuntimeError("Snapshot belongs to another robot")
    saved = snapshot["schedule"]["data"]
    events = writable_events(saved["events"])
    expected = event_counts(events)

    def confirm(enabled=None):
        for _ in range(5):
            try:
                current = command("getSchedule")["data"]
                if event_counts(current["events"]) == expected and (enabled is None or current["enabled"] == enabled):
                    return True
            except Exception:
                pass
            time.sleep(1)
        return False

    # An HTTP error can occur after application: reconcile using reads, never
    # automatically resend writes when their outcome is uncertain.
    try:
        command("setSchedule", {"events": events})
    except Exception as error:
        print(f"Schedule write returned an error; checking readback: {error}")
    if not confirm():
        raise RuntimeError("Schedule restore was not confirmed. Inspect the current schedule before another write.")
    try:
        command("enableSchedule" if saved["enabled"] else "disableSchedule")
    except Exception as error:
        print(f"Schedule toggle returned an error; checking readback: {error}")
    if not confirm(saved["enabled"]):
        raise RuntimeError("Schedule enabled state was not confirmed. Check it in the app.")
    print("Original schedule and enabled state confirmed by readback")


if __name__ == "__main__":
    main()
