# Sourced by bash: export the settings the manager page keeps in
# ~/.config/opsbox/env, so `docker exec` shells and the remote-control command
# see them as well as the sessions the manager starts.
#
# Read line by line and exported as text, never sourced: a value such as
# $(...) must stay a value, not run.
if [ -r "$HOME/.config/opsbox/env" ]; then
  while IFS='=' read -r opsbox_name opsbox_value; do
    case "$opsbox_name" in
      '' | \#*) continue ;;
    esac
    export "$opsbox_name=$opsbox_value"
  done < "$HOME/.config/opsbox/env"
  unset opsbox_name opsbox_value
fi
