#!/system/bin/sh

FP_ID=$(cat /proc/fp_id 2>/dev/null || true)

# Helper for safe atomic write to proc/sys nodes
safe_write_proc() {
    local target="$1"
    shift
    local payload="$*"
    if [ -e "$target" ]; then
        echo "$payload" > "$target" 2>/dev/null || true
    fi
}

# setprop *boot* property because we dont like resetting it
# ro.build.product overrides to support non-unified custom ROMs
# ro.product.product.device overrides to support realme ui 1 flash

if grep -q androidboot.prjname /proc/cmdline; then
    echo "custom-script: Detected realme UI 2 firmware" > /tmp/recovery.log
    resetprop ro.device.latest_fw true
else
    echo "custom-script: Detected realme UI 1 firmware" > /tmp/recovery.log
    resetprop ro.device.latest_fw false
fi

  setprop ro.boot.prjname 19605
  resetprop ro.build.product RMX1901
  resetprop ro.product.product.device $(getprop ro.build.product)
