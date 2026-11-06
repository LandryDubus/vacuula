# Reversible D7 trial: PRs #98–#104

This branch, `test/d7-pr98-104-integration`, combines all seven PR heads. It retains the previously resolved scheduling/build conflicts: multi-day expansion and validation feed the confirmed-save controller; the drawer closes only after robot readback confirms the complete schedule. No firmware, installer, or robot endpoint configuration is changed by the build.

It now also includes the separate `fix/gen3-schedule-timezone` correction. A real D7 run showed that an event displayed as 11:00 Europe/Paris ran at 12:00 during UTC+01:00. Upstream commit `871c3a2` had removed UTC/local conversion before PRs #98–#104. The correction restores display conversion and converts only newly entered events to UTC before saving; existing robot events remain unchanged. An existing 11:00 UTC event now displays as 12:00 Paris time. A new 11:00 Paris event is stored as 10:00 UTC in winter (09:00 UTC in summer). Weekdays shift with the time when conversion crosses midnight. Review schedules after daylight-saving changes: the robot stores fixed UTC repetitions, and this correction does not automatically rewrite them at seasonal transitions.

The integration branch also fixes Docker version-file generation to parse `pyproject.toml` with Python instead of `sed`, so both CRLF and LF checkouts produce valid Python.

The instructions below are for Docker Compose on **64-bit Raspberry Pi OS**. Run commands in Bash on the Pi, in the same shell unless indicated otherwise. They use a separate checkout, image tag, Compose project, and **copy** of the entire data directory. Your original container, image, and mounted data are retained for rollback. Nothing is deployed automatically.

Docker rollback protects server data, not settings stored on the robot. Schedule writes, boundary edits, cleaning, exploration, and map deletion can affect the real D7. The snapshot tool backs up schedules, preferences, map metadata and boundaries, but only schedule restoration is automated. Do not delete your only useful map or edit its boundaries during the first trial. A deleted Gen3 map may remain unavailable to the robot until it refreshes its map list after rollback; reconnection behavior still needs physical verification. This is a controlled trial, not a guarantee of zero risk.

## 1. Build while production keeps running

Use a fresh checkout, outside the directory containing your production Compose file:

```bash
git clone --single-branch --branch test/d7-pr98-104-integration \
  https://github.com/LandryDubus/vacuula.git "$HOME/vacuula-d7-integration"
cd "$HOME/vacuula-d7-integration"
git rev-parse HEAD
uname -m                         # should report aarch64
docker compose version
docker buildx version

COMMIT=$(git rev-parse --short=12 HEAD)
IMAGE="vacuula:d7-trial-$COMMIT"
docker buildx build --platform linux/arm64 --load \
  -f build/docker/Dockerfile -t "$IMAGE" .
docker image inspect "$IMAGE" --format '{{.Os}}/{{.Architecture}}'
```

This builds on the Pi; cross-compiling on a PC is optional. `--load` keeps the image local. Do not replace your production image tag or push a release tag. Keep sufficient free disk space for the image, copied data, and backup archive (`df -h` and `docker system df`).

## 2. Identify and back up the current installation

Find the **existing** production container; substitute its real name below. The serial is available in the app's robot settings or `GET /api/robots`. Adjust the URL if your current app uses another port.

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
LIVE='replace-with-existing-vacuula-container-name'
SERIAL='replace-with-your-D7-serial'
URL='http://127.0.0.1:8050'

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
TRIAL="$HOME/vacuula-d7-trial-$STAMP"
umask 077
mkdir -m 700 "$TRIAL"
docker inspect "$LIVE" > "$TRIAL/production-container.json"
printf '%s\n' "$LIVE" > "$TRIAL/production-container-name"
printf '%s\n' "$IMAGE" > "$TRIAL/trial-image"
printf '%s\n' "$SERIAL" > "$TRIAL/robot-serial"
printf '%s\n' "$URL" > "$TRIAL/server-url"

python3 build/docker/d7-robot-state.py backup \
  --url "$URL" --serial "$SERIAL" --file "$TRIAL/robot-before.json"
```

The backup command issues only read commands. If it fails, resolve the failure before continuing. Also take screenshots of your existing schedules and maps. The trial directory contains robot credentials, the server TLS private key and other private data; keep it private and copy the finished backup to another machine if possible.

Generate the trial environment from the original container's server settings. This check deliberately refuses unsupported installation layouts or an already stopped production container. A standard deployment has a persistent mount at `/server/data` and host networking.

```bash
python3 - "$TRIAL" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
container = json.loads((root / 'production-container.json').read_text())[0]
assert container['State']['Running'], 'Production must be running at snapshot time'
assert container['HostConfig']['NetworkMode'] == 'host', 'Review networking before using this procedure'
mounts = container['Mounts']
assert any(m['Destination'] == '/server/data' for m in mounts), 'Persistent data mount not found'
assert not any(m['Destination'].startswith('/server/data/') for m in mounts), 'Nested data mounts need a custom backup'
allowed = {'GEN3_PORT', 'GEN4_PORT', 'VACUULA_PORT', 'GEN3_ENABLED', 'GEN4_ENABLED',
           'MDNS_ENABLED', 'TZ', 'HOST_IP', 'LOG_LEVEL', 'HA_INGRESS'}
env = dict(item.split('=', 1) for item in container['Config']['Env'] if '=' in item)
env = {key: value for key, value in env.items() if key in allowed}
env.update(GEN3_ENABLED='True', GEN4_ENABLED='False', HA_INGRESS='False')
assert all('\n' not in value and '$' not in value and '#' not in value for value in env.values())
(root / 'server.env').write_text(''.join(f'{k}={v}\n' for k, v in sorted(env.items())))
(root / 'production-image-id').write_text(container['Image'] + '\n')
print('Original settings captured; Gen4 listener disabled for this D7-only trial.')
PY

docker image tag "$(cat "$TRIAL/production-image-id")" "vacuula:d7-rollback-$STAMP"
printf 'VACUULA_TRIAL_DIR=%s\nVACUULA_TRIAL_IMAGE=%s\n' "$TRIAL" "$IMAGE" > "$TRIAL/compose.env"
```

Save your production Compose YAML and its `.env` alongside this backup manually if you want protection against accidental later edits. The original stopped container is the primary rollback target; no Compose configuration is replaced here. If another robot also depends on this server, plan for its downtime before continuing.

## 3. Stop production and make a consistent data copy

Choose a time outside scheduled cleanings, with the D7 idle and docked. Stop any updater/automation that might restart or replace the production container. Do not reboot the Pi during the trial: an original container with restart policy `always` may otherwise start again and contend for the same ports.

```bash
docker stop "$LIVE"
test "$(docker inspect --format '{{.State.Running}}' "$LIVE")" = false
mkdir "$TRIAL/data"
docker cp "$LIVE:/server/data/." "$TRIAL/data/"

docker run --rm --network none --entrypoint python \
  --mount "type=bind,source=$TRIAL/data,target=/source,readonly" \
  --mount "type=bind,source=$TRIAL,target=/backup" \
  "$IMAGE" -c 'import tarfile; a=tarfile.open("/backup/original-data.tar.gz", "x:gz"); a.add("/source", arcname="."); a.close()'

docker run --rm --network none --entrypoint python \
  --mount "type=bind,source=$TRIAL/data,target=/source,readonly" \
  "$IMAGE" -c 'import pathlib,sqlite3; p=pathlib.Path("/source"); assert (p/"ssl/cert.pem").is_file(); assert (p/"ssl/privkey.pem").is_file(); c=sqlite3.connect("file:/source/vacuula.db?mode=ro",uri=True); assert c.execute("PRAGMA integrity_check").fetchall()==[("ok",)]; print("Copied database and TLS identity verified")'
```

Stopping before copying makes the SQLite database, any WAL files, uploads and TLS identity a consistent set. The trial writes only under `$TRIAL/data`. Its migrations never touch the original volume. If any command fails after the stop, use the rollback steps immediately; do not proceed with a partial copy. Never run the trial image against the production mount, and never copy its migrated database over the original.

## 4. Smoke-test with no access to the robot

```bash
SMOKE="vacuula-d7-smoke-$STAMP"
docker run -d --name "$SMOKE" --network none \
  --env-file "$TRIAL/server.env" \
  -e GEN3_ENABLED=False -e GEN4_ENABLED=False -e MDNS_ENABLED=False \
  -e HOST_IP=0.0.0.0 \
  --mount "type=bind,source=$TRIAL/data,target=/server/data" "$IMAGE"
docker logs "$SMOKE"
docker exec "$SMOKE" python -c \
  'import os,urllib.request; print(urllib.request.urlopen("http://127.0.0.1:"+os.environ.get("VACUULA_PORT","8050")+"/api/server",timeout=5).read().decode())'
```

Startup may take a few seconds; repeat the **read-only** exec check if it ran before startup finished. The logs should show a completed migration and the `/api/server` request must succeed. `--network none` and disabled robot listeners keep this phase disconnected from your D7.

```bash
docker stop "$SMOKE"
docker rm "$SMOKE"
```

## 5. Switch the D7 to the trial by starting the server

No installer, USB setup, firmware flash, Wi-Fi reset, new certificate, or robot endpoint change is needed. The test uses the same Pi, ports, robot credentials and copied TLS certificate. Keep the original container stopped throughout this phase.

```bash
test "$(docker inspect --format '{{.State.Running}}' "$LIVE")" = false
docker compose --env-file "$TRIAL/compose.env" \
  -f build/docker/docker-compose.d7-trial.yaml -p vacuula-d7-trial config
docker compose --env-file "$TRIAL/compose.env" \
  -f build/docker/docker-compose.d7-trial.yaml -p vacuula-d7-trial up -d
docker compose --env-file "$TRIAL/compose.env" \
  -f build/docker/docker-compose.d7-trial.yaml -p vacuula-d7-trial logs --tail 100
```

Open your usual app URL and hard-refresh it. Allow the D7 time to reconnect. If it does not reconnect, roll back; do not rerun the installer as a troubleshooting step.

Test in increasing order of impact:

1. Read-only: robot status, existing schedules, map list, boundaries and historical runs. Verify the original schedules and zone names are intact.
2. Form-only: select multiple weekdays, select/clear a zone, and cancel without saving.
3. Schedule writes: take the saved snapshot first, then create a temporary event sufficiently far in the future and remove it. Verify the full schedule after every operation. Schedule editing rewrites the robot's complete schedule. Test without leaving a temporary cleaning due soon.
4. Optional cleaning: a small supervised run in a clear area, after checking maps and no-go boundaries. Observe its start and docking behavior.
5. Last, if you accept the separate robot-map risk: test deletion on a disposable map. Preserve your useful map and no-go boundaries. Rolling back the server does not guarantee reversal of firmware-side effects or cached map availability.

Stop and roll back on unexpected movement, changed boundaries, unexplained schedule changes or repeated command failures. Use the robot's physical controls to stop movement if the app is unavailable. Avoid factory reset, firmware updates and re-pairing throughout the trial.

## 6. Roll back without database downgrades

From the integration checkout, with the variables above still set:

```bash
docker compose --env-file "$TRIAL/compose.env" \
  -f build/docker/docker-compose.d7-trial.yaml -p vacuula-d7-trial down
docker start "$LIVE"
docker logs --tail 100 "$LIVE"
```

That starts the exact original container, using its original image and untouched data. It does not pull `latest`, recreate the service, or run a database downgrade. Hard-refresh the app and verify reconnection, maps, boundaries, schedules and history. Data generated during the trial remains in the trial directory, not in the restored server.

If you changed schedules, wait for the D7 to reconnect and review its current schedule. To explicitly restore the saved schedule and enabled state:

```bash
python3 build/docker/d7-robot-state.py restore-schedule \
  --url "$URL" --serial "$SERIAL" --file "$TRIAL/robot-before.json" \
  --confirm-restore "$SERIAL"
```

The restore sends the original writable schedule fields, converts returned `boundary.id` values to `boundaryId`, and confirms events and enabled state by readback. It never automatically resends an uncertain write. If confirmation fails, inspect the schedule in the app before sending another write. Preferences, firmware, boundaries, map cache and physical cleaning outcomes are not automatically reverted by this command.

If you open a new terminal, restore the variables from your private trial directory first:

```bash
TRIAL="$HOME/replace-with-your-vacuula-d7-trial-directory"
LIVE=$(cat "$TRIAL/production-container-name")
SERIAL=$(cat "$TRIAL/robot-serial")
URL=$(cat "$TRIAL/server-url")
```

Keep the original container, rollback image and backups until you are satisfied. Do not run `docker compose down -v`, remove the production volume, prune images/volumes, or overwrite production data as part of this trial.

References: [Docker volume backup/restore](https://docs.docker.com/engine/storage/volumes/), [ARM64 builds](https://docs.docker.com/build/building/multi-platform/), [host networking](https://docs.docker.com/engine/network/drivers/host/).

## Verification performed before publishing this branch

- All seven PR head commits are ancestors of the integration branch.
- Strict frontend TypeScript checking and production bundling pass.
- Timezone correction: 4,704 temporary UTC/local roundtrip checks passed across seven offsets and all weekdays, including midnight/week boundaries. Checks against the actual multi-weekday Save callback and confirmation controller passed for winter/summer offsets, unchanged existing UTC events, preserved zone assignments, and invalid-form blocking. The corrected build still needs a physical timed run; the previous physical run confirmed zone cleaning and exposed the timing bug.
- The repository Dockerfile builds a `linux/arm64` image successfully.
- The ARM64 image runs under local emulation with networking disabled, upgrades a simulated D7 database from the previous schema, serves `/api/server`, and retains the existing map and cleaning history. Startup retains the existing TLS certificate and key.
- Compose configuration validates. The snapshot helper passes temporary simulated checks for read-only capture, private file permissions, refusing overwrites, requiring explicit restoration, preserving map/zone IDs, and reconciling a lost write reply without duplicate writes.
- No physical D7 or production data was used in these checks. Raspberry Pi hardware, your installed robot firmware, and real schedule/map behavior still need the supervised trial above.
