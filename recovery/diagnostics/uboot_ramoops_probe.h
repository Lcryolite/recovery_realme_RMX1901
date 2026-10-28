/* SPDX-License-Identifier: GPL-2.0+ */
#ifndef RMX1901_UBOOT_RAMOOPS_PROBE_H
#define RMX1901_UBOOT_RAMOOPS_PROBE_H

/* RMX1901 Recovery DTB: 4 MiB arena, five 256 KiB dump slots. */
#define RMX1901_RAMOOPS_BASE 0xb7e00000UL
#define RMX1901_RAMOOPS_SIZE 0x00400000UL
#define RMX1901_CONSOLE_OFFSET 0x00140000UL
#define RMX1901_CONSOLE_SIZE 0x00040000UL
#define RMX1901_PERSISTENT_SIG 0x43474244U
#define RMX1901_PROBE_MARKER "RMX_PSTORE_PROBE_20261028_8f5b"

struct rmx1901_persistent_buffer {
	u32 sig;
	u32 start;
	u32 size;
	u8 data[];
};

/* Return -1 for an invalid/empty header, 0 for no marker, or 1 for a match. */
static inline int rmx1901_console_has_probe(const volatile struct rmx1901_persistent_buffer *buffer)
{
	const char *marker = RMX1901_PROBE_MARKER;
	u32 size = buffer->size, start = buffer->start;
	unsigned int i, j, marker_size = sizeof(RMX1901_PROBE_MARKER) - 1;

	if (buffer->sig != RMX1901_PERSISTENT_SIG || !size ||
	    size > RMX1901_CONSOLE_SIZE - sizeof(*buffer) || start > size)
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

#endif
