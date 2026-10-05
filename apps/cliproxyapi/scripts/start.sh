#!/bin/sh
set -eu
umask 077

: "${PIER_DATA_DIR:?PIER_DATA_DIR must be set by pier-agent}"
case "$PIER_DATA_DIR" in
    /*) ;;
    *) echo 'PIER_DATA_DIR must be an absolute path' >&2; exit 1 ;;
esac

release_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
runtime_dir="$PIER_DATA_DIR/runtime"
config_file="$runtime_dir/config.yaml"
release_marker="$runtime_dir/pier-release"

mkdir -p "$runtime_dir" "$PIER_DATA_DIR/auths" "$PIER_DATA_DIR/static"

# Release paths include the deployment ID in Pier, even for the same package.
# Keep panel edits across ordinary restarts; redeploy and rollback use the
# configuration from the selected package. Credentials live outside runtime/.
previous_release=''
if [ -f "$release_marker" ]; then
    previous_release=$(cat "$release_marker")
fi
if [ "$previous_release" != "$release_dir" ] || [ ! -f "$config_file" ]; then
    staging_dir=$(mktemp -d "$runtime_dir/.init.XXXXXX")
    cleanup() {
        rm -f "$staging_dir/config.yaml" "$staging_dir/pier-release"
        rmdir "$staging_dir"
    }
    trap cleanup EXIT
    trap 'exit 1' HUP INT TERM

    cp "$release_dir/configs/config.yaml" "$staging_dir/config.yaml"
    test -s "$staging_dir/config.yaml"
    chmod 600 "$staging_dir/config.yaml"
    printf '%s\n' "$release_dir" > "$staging_dir/pier-release"
    mv -f "$staging_dir/config.yaml" "$config_file"
    # Commit the marker last: interrupted initialization is retried on restart.
    mv -f "$staging_dir/pier-release" "$release_marker"

    cleanup
    trap - EXIT HUP INT TERM
fi

export WRITABLE_PATH="$PIER_DATA_DIR"
export MANAGEMENT_STATIC_PATH="$PIER_DATA_DIR/static"
cd "$PIER_DATA_DIR"
exec "$release_dir/bin/cli-proxy-api" -config "$config_file" "$@"

