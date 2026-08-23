# /system/etc/bash.bashrc - Recovery environment bash configuration

# Global atomic printf wrapper
printf() {
    local _raw _buf
    _raw=$(builtin printf "$@" 2>/dev/null || command -p printf "$@" 2>/dev/null || toybox printf "$@" 2>/dev/null || busybox printf "$@" 2>/dev/null; echo -n "_EOB_")
    _buf="${_raw%_EOB_}"
    builtin printf "%s" "$_buf" 2>/dev/null || command -p printf "%s" "$_buf"
}

# Helper for safe atomic write to proc/sys nodes
safe_write_proc() {
    local target="$1"
    shift
    local payload="$*"
    if [ -e "$target" ]; then
        printf '%s\n' "$payload" > "$target" 2>/dev/null || true
    fi
}
