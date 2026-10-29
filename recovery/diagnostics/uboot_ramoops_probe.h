/* SPDX-License-Identifier: GPL-2.0+ */
#ifndef RMX1901_UBOOT_RAMOOPS_PROBE_H
#define RMX1901_UBOOT_RAMOOPS_PROBE_H

/* RMX1901 Recovery DTB: 4 MiB arena, five 256 KiB dump slots. */
#define RMX1901_RAMOOPS_BASE 0xb7e00000UL
#define RMX1901_RAMOOPS_SIZE 0x00400000UL
#define RMX1901_DUMP_SIZE 0x00040000UL
#define RMX1901_DUMP_SLOTS 5U
#define RMX1901_CONSOLE_OFFSET 0x00140000UL
#define RMX1901_CONSOLE_SIZE 0x00040000UL
#define RMX1901_PERSISTENT_SIG 0x43474244U
#define RMX1901_PROBE_MARKER "RMX_PSTORE_PROBE_20261028_8f5b"
#define RMX1901_PANIC_MARKER "RMX_PANIC_PROBE_20261029j"

struct rmx1901_persistent_buffer {
	u32 sig;
	u32 start;
	u32 size;
	u8 data[];
};

/* Inspect raw bytes only; a compressed crash record may hide a valid marker. */
/* Return -1 for invalid/empty input, 0 for no raw match, or 1 for a match. */
static inline int rmx1901_record_has_marker(
	const volatile struct rmx1901_persistent_buffer *buffer,
	unsigned int region_size, const char *marker, unsigned int marker_size)
{
	u32 size, start;
	unsigned int i, j;

	if (!marker || !marker_size || region_size <= sizeof(*buffer) ||
	    region_size > RMX1901_RAMOOPS_SIZE)
		return -1;
	size = buffer->size;
	start = buffer->start;

	if (buffer->sig != RMX1901_PERSISTENT_SIG || !size ||
	    size > region_size - sizeof(*buffer) || start > size)
		return -1;
	if (size < marker_size)
		return 0;
	for (i = 0; i + marker_size <= size; i++) {
		for (j = 0; j < marker_size; j++) {
			if (buffer->data[(start + i + j) % size] != (u8)marker[j])
				break;
		}
		if (j == marker_size)
			return 1;
	}
	return 0;
}

static inline int rmx1901_console_has_probe(const volatile struct rmx1901_persistent_buffer *buffer)
{
	return rmx1901_record_has_marker(buffer, RMX1901_CONSOLE_SIZE,
		RMX1901_PROBE_MARKER, sizeof(RMX1901_PROBE_MARKER) - 1);
}

#endif
