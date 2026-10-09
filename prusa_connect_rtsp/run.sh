#!/usr/bin/with-contenv bashio

CONFIG_PATH=/data/options.json
FINGERPRINT_DIR=/data/fingerprints
PIDS=()
STOPPING=false

# Create fingerprint storage directory
mkdir -p "$FINGERPRINT_DIR"

wait_for_process() {
    local pid="$1" status
    while true; do
        if wait "$pid"; then
            return 0
        else
            status=$?
        fi
        # A trap interrupts wait before the child has finished.
        if ! kill -0 "$pid" 2>/dev/null; then
            return "$status"
        fi
    done
}

shutdown() {
    STOPPING=true
    bashio::log.info "Stop signal received; shutting down cameras..."
    local pid
    for pid in "${PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill -TERM "$pid"
        fi
    done
}
trap shutdown SIGTERM SIGINT

load_fingerprint() {
    local token="$1" slug="$2" configured="$3"
    local token_hash token_file legacy_file
    token_hash=$(printf '%s' "$token" | sha1sum | awk '{print $1}')
    if [[ ! "$token_hash" =~ ^[0-9a-f]{40}$ ]]; then
        bashio::log.error "Could not compute fingerprint storage key"
        return 1
    fi
    token_file="$FINGERPRINT_DIR/token_${token_hash}.txt"
    legacy_file="$FINGERPRINT_DIR/${slug}.txt"
    if [ -n "$configured" ]; then
        FINGERPRINT="$configured"
        printf '%s\n' "$FINGERPRINT" > "$token_file" || return 1
    elif [ -f "$token_file" ]; then
        FINGERPRINT=$(cat "$token_file") || return 1
    elif [ -f "$legacy_file" ]; then
        FINGERPRINT=$(cat "$legacy_file") || return 1
        printf '%s\n' "$FINGERPRINT" > "$token_file" || return 1
        bashio::log.info "Migrated fingerprint for ${slug} to token-keyed storage"
    else
        FINGERPRINT=$(python3 -c 'import secrets; print(secrets.token_hex(20))') || return 1
        printf '%s\n' "$FINGERPRINT" > "$token_file" || return 1
        bashio::log.info "Generated fingerprint for ${slug}"
    fi
}

run_camera() {
    local child_pid="" status
    stop_camera() {
        STOPPING=true
        if [ -n "$child_pid" ] && kill -0 "$child_pid" 2>/dev/null; then
            kill -TERM "$child_pid"
        fi
    }
    trap stop_camera SIGTERM SIGINT
    while [ "$STOPPING" = false ]; do
        python3 -u /main.py > >(
            while IFS= read -r line; do
                bashio::log.info "[${CAMERA_NAME}] ${line}"
            done
        ) 2>&1 &
        child_pid=$!
        if [ "$STOPPING" = true ]; then stop_camera; fi
        if wait_for_process "$child_pid"; then status=0; else status=$?; fi
        child_pid=""
        if [ "$STOPPING" = true ]; then break; fi
        bashio::log.warning "[${CAMERA_NAME}] camera process exited (${status}); restarting in 10s"
        sleep 10 &
        child_pid=$!
        if [ "$STOPPING" = true ]; then stop_camera; fi
        wait_for_process "$child_pid" || true
        child_pid=""
    done
}

# Test Python and dependencies
bashio::log.info "Testing Python environment..."
if ! python3 -c "import cv2; import requests; print('Dependencies OK')" 2>&1; then
    bashio::log.error "Python dependencies failed to load!"
    bashio::log.error "Try rebuilding the addon."
    exit 1
fi
bashio::log.info "Python environment ready"

# Detect MQTT broker from Home Assistant services
if bashio::services.available "mqtt"; then
    MQTT_HOST=$(bashio::services mqtt "host")
    MQTT_PORT=$(bashio::services mqtt "port")
    MQTT_USER=$(bashio::services mqtt "username")
    MQTT_PASS=$(bashio::services mqtt "password")
    bashio::log.info "MQTT broker found at ${MQTT_HOST}:${MQTT_PORT}"
    export MQTT_HOST MQTT_PORT MQTT_USER MQTT_PASS
else
    bashio::log.warning "MQTT broker not available - camera entities will not be created in Home Assistant"
    bashio::log.warning "Install Mosquitto broker addon for HA camera entity support"
fi

# Get number of cameras
CAMERAS_COUNT=$(jq '.cameras | length' $CONFIG_PATH)
bashio::log.info "Found ${CAMERAS_COUNT} camera(s) configured"

# Iterate over each camera
for (( i=0; i<CAMERAS_COUNT; i++ )); do
    if [ "$STOPPING" = true ]; then break; fi
    CAMERA_NAME=$(jq -r ".cameras[$i].name" $CONFIG_PATH)
    CAMERA_SLUG="${CAMERA_NAME// /_}"

    TOKEN=$(jq -r ".cameras[$i].token" $CONFIG_PATH)
    CONFIGURED_FINGERPRINT=$(jq -r ".cameras[$i].fingerprint // empty" $CONFIG_PATH)
    if ! load_fingerprint "$TOKEN" "$CAMERA_SLUG" "$CONFIGURED_FINGERPRINT"; then
        bashio::log.error "Could not store fingerprint for ${CAMERA_NAME}"
        shutdown
        for pid in "${PIDS[@]}"; do wait_for_process "$pid" || true; done
        exit 1
    fi

    # Export environment variables for Python script
    export CAMERA_NAME="$CAMERA_NAME"
    export CAMERA_SLUG="$CAMERA_SLUG"
    export RTSP_URL=$(jq -r ".cameras[$i].rtsp_url" $CONFIG_PATH)
    export TOKEN=$(jq -r ".cameras[$i].token" $CONFIG_PATH)
    export FINGERPRINT
    export UPLOAD_INTERVAL=$(jq -r ".cameras[$i].upload_interval" $CONFIG_PATH)
    export ENABLE_TIMELAPSE=$(jq -r ".cameras[$i].timelapse_enabled" $CONFIG_PATH)
    export TIMELAPSE_SAVE_INTERVAL=$(jq -r ".cameras[$i].timelapse_save_interval // 30" $CONFIG_PATH)
    export TIMELAPSE_FPS=$(jq -r ".cameras[$i].timelapse_fps // 24" $CONFIG_PATH)
    export TIMELAPSE_DIR="/share/prusa_connect_rtsp/${CAMERA_SLUG}"

    mkdir -p "$TIMELAPSE_DIR"

    # Mask any credentials embedded in the RTSP URL before logging it.
    # Host/port/path stay visible so connection problems remain diagnosable.
    RTSP_URL_SAFE=$(echo "$RTSP_URL" | sed 's|://[^/@]*@|://***:***@|')

    # Log configuration details
    bashio::log.info "-------------------------------------------"
    bashio::log.info "Camera ${i}: ${CAMERA_NAME}"
    bashio::log.info "  RTSP URL: ${RTSP_URL_SAFE}"
    bashio::log.info "  Fingerprint: ${FINGERPRINT}"
    bashio::log.info "  Token: ${TOKEN:0:8}... (hidden)"
    bashio::log.info "  Upload interval: ${UPLOAD_INTERVAL}s"
    bashio::log.info "  Timelapse: ${ENABLE_TIMELAPSE}"
    bashio::log.info "-------------------------------------------"

    bashio::log.info "Starting camera: ${CAMERA_NAME}"
    # Use unbuffered Python output (-u) for real-time logging
    run_camera &
    PIDS+=($!)
done

# Wait for all processes
if [ "$STOPPING" = true ]; then shutdown; fi
for pid in "${PIDS[@]}"; do
    wait_for_process "$pid" || bashio::log.warning "Camera wrapper ${pid} exited with an error"
done
bashio::log.info "All cameras stopped."
