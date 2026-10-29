LOCAL_PATH := $(call my-dir)

ifeq ($(TARGET_DEVICE),RMX1901)
include $(LOCAL_PATH)/recovery/diagnostics/Android.mk
endif
