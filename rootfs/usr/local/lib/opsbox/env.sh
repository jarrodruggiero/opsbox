# shellcheck shell=bash
# Sourced by bash: export the settings the manager page keeps in
# ~/.config/opsbox/env, so `docker exec` shells and the remote-control command
# see them as well as the sessions the manager starts.
#
# Read line by line and exported as text, never sourced: a value such as
# $(...) must stay a value, not run. Split with parameter expansion, not IFS,
# because read drops a trailing = and base64 tokens end in them.
if [ -r "$HOME/.config/opsbox/env" ]; then
  while IFS= read -r opsbox_line; do
    case "$opsbox_line" in
      [A-Za-z_]*=*)
        opsbox_name="${opsbox_line%%=*}"
        case "$opsbox_name" in
          *[!A-Za-z0-9_]*) ;;
          # Tried in a subshell first: exporting a readonly variable (UID, or one of
          # the entrypoint's) aborts a sourced file outright, whatever || says.
          *)
            if (export "$opsbox_name=${opsbox_line#*=}") 2>/dev/null; then
              export "$opsbox_name=${opsbox_line#*=}"
            fi
            ;;
        esac
        ;;
    esac
  done < "$HOME/.config/opsbox/env"
  unset opsbox_line opsbox_name
fi
