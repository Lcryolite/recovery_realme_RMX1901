/* SPDX-License-Identifier: GPL-2.0+ */
#ifndef RMX1901_UBOOT_LOGFS_GUARD_H
#define RMX1901_UBOOT_LOGFS_GUARD_H

/* Geometry verified on the owner's RMX1901; fail closed on any change. */
static inline int rmx1901_logfs_target_valid(unsigned int matches, int device, int partition,
                                             unsigned int target, unsigned int lun,
                                             unsigned long long start, unsigned long long blocks,
                                             unsigned long block_size, int fat)
{
	return matches == 1 && device == 4 && partition == 31 && target == 0 && lun == 4 && start == 652177 &&
	       blocks == 2048 && block_size == 4096 && fat == 1;
}

#endif
