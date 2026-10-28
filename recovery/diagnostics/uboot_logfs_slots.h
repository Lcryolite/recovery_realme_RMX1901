/* SPDX-License-Identifier: GPL-2.0+ */
#ifndef RMX1901_UBOOT_LOGFS_SLOTS_H
#define RMX1901_UBOOT_LOGFS_SLOTS_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

struct rmx_logfs_slot {
    bool exists;
    bool owned;
    uint64_t generation;
};

#if defined(CONFIG_RMX1901_FAT_EXCLUSIVE_TEST)
void rmx_logfs_allow_replace(const char *name);
#endif

static inline bool rmx_logfs_record_generation(const char *data, size_t length,
                                                 unsigned slot, uint64_t *generation)
{
    static const char prefix[] = "RMX1901_UBOOT_LOG v1\ncandidate=";
    static const char complete[] = "console_overflow=0\n\n";
    static const char truncated[] = "console_overflow=1\n\n";
    static const char version[] = "RMX1901_LOGFS_FORMAT=2\nsequence=";
    static const char legacy1[] = "usb-logfs-write-20261028e";
    static const char legacy2[] = "usb-logfs-fixed-20261028g";
    const char *end = data + length, *candidate, *newline, *body;
    if (length < sizeof(prefix) || length > 32768 || memchr(data, '\0', length) ||
        memcmp(data, prefix, sizeof(prefix) - 1)) return false;
    candidate = data + sizeof(prefix) - 1;
    newline = memchr(candidate, '\n', (size_t)(end - candidate));
    if (!newline || newline == candidate || newline - candidate > 68) return false;
    for (const char *p = candidate; p < newline; p++)
        if (!((*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') ||
              (*p >= '0' && *p <= '9') || *p == '-' || *p == '_')) return false;
    body = newline + 1;
    if ((size_t)(end - body) < sizeof(complete) - 1 ||
        (memcmp(body, complete, sizeof(complete) - 1) &&
         memcmp(body, truncated, sizeof(truncated) - 1))) return false;
    body += sizeof(complete) - 1;
    if (body == end) return false;
    if ((slot == 0 && (size_t)(newline - candidate) == sizeof(legacy1) - 1 &&
         !memcmp(candidate, legacy1, sizeof(legacy1) - 1)) ||
        (slot == 1 && (size_t)(newline - candidate) == sizeof(legacy2) - 1 &&
         !memcmp(candidate, legacy2, sizeof(legacy2) - 1))) {
        *generation = 0;
        return true;
    }
    if ((size_t)(end - body) < sizeof(version) || memcmp(body, version, sizeof(version) - 1))
        return false;
    const char *p = body + sizeof(version) - 1;
    uint64_t value = 0;
    bool digits = false;
    while (p < end && *p >= '0' && *p <= '9') {
        unsigned digit = (unsigned)(*p++ - '0');
        if (value > (UINT64_MAX - digit) / 10) return false;
        value = value * 10 + digit;
        digits = true;
    }
    if (!digits || !value || p == end || *p++ != '\n' || p == end) return false;
    *generation = value;
    return true;
}

static inline int rmx_logfs_select(const struct rmx_logfs_slot slots[3],
                                   unsigned *selected, uint64_t *generation)
{
    uint64_t newest = 0;
    unsigned target = 3;
    for (unsigned i = 0; i < 3; i++) {
        if (slots[i].exists && !slots[i].owned) return -1;
        if (slots[i].exists && slots[i].generation > newest) newest = slots[i].generation;
        if (!slots[i].exists && target == 3) target = i;
    }
    if (newest == UINT64_MAX) return -1;
    if (target == 3) {
        target = 0;
        for (unsigned i = 1; i < 3; i++)
            if (slots[i].generation < slots[target].generation) target = i;
    }
    *selected = target;
    *generation = newest + 1;
    return 0;
}

#endif
