# Home Assistant Add-on: Prusa Connect RTSP Camera

[![Open your Home Assistant instance and show the add add-on repository dialog](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fstepman0%2FPrusa-Connect-RTSP-HA)

Stream RTSP camera feeds to Prusa Connect for 3D printer monitoring through Home Assistant's configuration UI.

## Features

- **Multi-camera support**: Configure multiple cameras, each with its own Prusa Connect credentials
- **Camera names in Prusa Connect**: Register the configured name when the camera connects
- **Automatic recovery**: Retry unreachable cameras and restart crashed camera processes independently
- **Stable fingerprints**: Preserve registration when a camera is renamed (use a separate token for each camera)
- **Password-protected tokens**: Prusa Connect tokens are hidden in the UI
- **Timelapse support**: Optional frame capture for timelapse video generation
- **Upstream drift tracking**: a GitHub Actions workflow reports when upstream
  Prusa-Connect-RTSP changes, for review and hand-merging — it never overwrites
  the add-on's source (see [`upstream/`](upstream/README.md))

## Installation

1. Add this repository to your Home Assistant Add-on Store:
   - Navigate to **Settings** > **Add-ons** > **Add-on Store**
   - Click the menu (three dots) in the top right corner
   - Select **Repositories**
   - Add: `https://github.com/stepman0/Prusa-Connect-RTSP-HA`

2. Find "Prusa Connect RTSP Camera" in the add-on store and click **Install**

3. Configure your cameras in the **Configuration** tab

4. Start the add-on

### Fork images and migration

This fork is based on [schmacka/Prusa-Connect-RTSP-HA](https://github.com/schmacka/Prusa-Connect-RTSP-HA).
It publishes its own versioned images at `ghcr.io/stepman0/rtsp-to-prusa-ha-{arch}`.

Before installing a release, verify that **Build and Push Docker Images** has
completed successfully in this fork's **Actions** tab. Fork workflows may need
to be enabled first. After the first build, set each `rtsp-to-prusa-ha-*` package
to **Public** in GitHub's package settings so Home Assistant can pull it without
credentials. Public repository visibility alone does not make packages public.

Refresh the Home Assistant Add-on Store after adding the fork repository.
If the original add-on is already installed, save its configuration and back up
any required timelapse files before migrating. Home Assistant treats the fork as
a separate add-on: stop the original, install the fork's add-on, copy the camera
configuration, and start the fork. Do not run both copies at the same time.

## Quick Start

1. Get your Prusa Connect token and fingerprint from [connect.prusa3d.com](https://connect.prusa3d.com)
2. Configure your RTSP camera URL
3. Start the add-on and check the logs

See [DOCS.md](DOCS.md) for detailed setup instructions.

## Configuration

```yaml
cameras:
  - name: "My Printer"
    rtsp_url: "rtsp://192.168.1.100:554/stream"
    token: "your-prusa-connect-token"
    fingerprint: "your-40-char-fingerprint"
    upload_interval: 5
    timelapse_enabled: false
```

## Troubleshooting

### Camera images don't appear in Prusa Connect while the printer is off

This is expected. Prusa Connect only accepts and displays webcam snapshots while
the associated printer is **online**. The add-on keeps capturing and uploading
frames regardless, but Prusa's server rejects them until the printer reconnects —
so the snapshot simply stops updating in Connect until the printer is back on.

To avoid flooding the log and hammering Prusa's endpoint during this time, the
add-on automatically backs off: after several consecutive rejected uploads it
progressively increases the interval between attempts (up to 5 minutes), logs a
single "backing off" message, and returns to the normal `upload_interval` with an
"uploads resumed" message as soon as the printer comes back online. No action is
needed — images resume on their own.

## Credits

This add-on wraps [Prusa-Connect-RTSP](https://github.com/Knopersikcuo/Prusa-Connect-RTSP) by Knopersikcuo.
Camera naming, MQTT compatibility, token-keyed fingerprints, and camera recovery
are adapted from [DariBer's fork](https://github.com/DariBer/Prusa-Connect-RTSP-HA),
with revised shutdown handling.

## License

This project is provided as-is. See the upstream project for license details.
