#include <level_zero/zes_api.h>

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

static const char *mode_name(zes_fan_speed_mode_t mode)
{
    switch (mode) {
    case ZES_FAN_SPEED_MODE_DEFAULT:
        return "default";
    case ZES_FAN_SPEED_MODE_FIXED:
        return "fixed";
    case ZES_FAN_SPEED_MODE_TABLE:
        return "table";
    default:
        return "unknown";
    }
}

static void print_result(const char *operation, ze_result_t result)
{
    if (result != ZE_RESULT_SUCCESS)
        printf("    %s: unavailable (0x%08x)\n", operation, (unsigned int)result);
}

static void inspect_fan(zes_fan_handle_t fan, uint32_t index)
{
    zes_fan_properties_t properties = {
        .stype = ZES_STRUCTURE_TYPE_FAN_PROPERTIES,
        .pNext = NULL,
    };
    zes_fan_config_t config = {
        .stype = ZES_STRUCTURE_TYPE_FAN_CONFIG,
        .pNext = NULL,
    };
    int32_t rpm = -1;
    int32_t percent = -1;
    ze_result_t result;

    printf("  Fan %u\n", index);

    result = zesFanGetProperties(fan, &properties);
    if (result == ZE_RESULT_SUCCESS) {
        printf("    canControl: %s\n", properties.canControl ? "yes" : "no");
        printf("    modes: default=%s fixed=%s table=%s\n",
               properties.supportedModes & (1U << ZES_FAN_SPEED_MODE_DEFAULT) ? "yes" : "no",
               properties.supportedModes & (1U << ZES_FAN_SPEED_MODE_FIXED) ? "yes" : "no",
               properties.supportedModes & (1U << ZES_FAN_SPEED_MODE_TABLE) ? "yes" : "no");
        printf("    units: rpm=%s percent=%s\n",
               properties.supportedUnits & (1U << ZES_FAN_SPEED_UNITS_RPM) ? "yes" : "no",
               properties.supportedUnits & (1U << ZES_FAN_SPEED_UNITS_PERCENT) ? "yes" : "no");
        printf("    maxRPM: %d\n", properties.maxRPM);
        printf("    maxPoints: %d\n", properties.maxPoints);
        printf("    subdevice: %s", properties.onSubdevice ? "yes" : "no");
        if (properties.onSubdevice)
            printf(" (%u)", properties.subdeviceId);
        putchar('\n');
    } else {
        print_result("properties", result);
    }

    result = zesFanGetConfig(fan, &config);
    if (result == ZE_RESULT_SUCCESS) {
        printf("    current mode: %s\n", mode_name(config.mode));
        printf("    fixed setting: %d (%s)\n",
               config.speedFixed.speed,
               config.speedFixed.units == ZES_FAN_SPEED_UNITS_PERCENT ? "percent" : "rpm");
        printf("    table points: %d\n", config.speedTable.numPoints);
    } else {
        print_result("configuration", result);
    }

    result = zesFanGetState(fan, ZES_FAN_SPEED_UNITS_RPM, &rpm);
    if (result == ZE_RESULT_SUCCESS)
        printf("    state: %d RPM\n", rpm);
    else
        print_result("RPM state", result);

    result = zesFanGetState(fan, ZES_FAN_SPEED_UNITS_PERCENT, &percent);
    if (result == ZE_RESULT_SUCCESS)
        printf("    state: %d percent\n", percent);
    else
        print_result("percent state", result);
}

int main(void)
{
    zes_driver_handle_t *drivers;
    uint32_t driver_count = 0;
    ze_result_t result;

    result = zesInit(0);
    if (result != ZE_RESULT_SUCCESS) {
        fprintf(stderr, "zesInit failed: 0x%08x\n", (unsigned int)result);
        return EXIT_FAILURE;
    }

    result = zesDriverGet(&driver_count, NULL);
    if (result != ZE_RESULT_SUCCESS) {
        fprintf(stderr, "zesDriverGet(count) failed: 0x%08x\n", (unsigned int)result);
        return EXIT_FAILURE;
    }

    printf("Sysman drivers: %u\n", driver_count);
    drivers = calloc(driver_count, sizeof(*drivers));
    if (driver_count && !drivers) {
        perror("calloc");
        return EXIT_FAILURE;
    }

    result = zesDriverGet(&driver_count, drivers);
    if (result != ZE_RESULT_SUCCESS) {
        fprintf(stderr, "zesDriverGet failed: 0x%08x\n", (unsigned int)result);
        free(drivers);
        return EXIT_FAILURE;
    }

    for (uint32_t driver_index = 0; driver_index < driver_count; ++driver_index) {
        zes_device_handle_t *devices;
        uint32_t device_count = 0;

        result = zesDeviceGet(drivers[driver_index], &device_count, NULL);
        if (result != ZE_RESULT_SUCCESS) {
            print_result("device enumeration", result);
            continue;
        }

        devices = calloc(device_count, sizeof(*devices));
        if (device_count && !devices) {
            perror("calloc");
            free(drivers);
            return EXIT_FAILURE;
        }

        result = zesDeviceGet(drivers[driver_index], &device_count, devices);
        if (result != ZE_RESULT_SUCCESS) {
            print_result("device handles", result);
            free(devices);
            continue;
        }

        for (uint32_t device_index = 0; device_index < device_count; ++device_index) {
            zes_device_properties_t properties = {
                .stype = ZES_STRUCTURE_TYPE_DEVICE_PROPERTIES,
                .pNext = NULL,
            };
            zes_fan_handle_t *fans;
            uint32_t fan_count = 0;

            result = zesDeviceGetProperties(devices[device_index], &properties);
            if (result == ZE_RESULT_SUCCESS)
                printf("Device %u:%u: %s (%s, driver %s)\n",
                       driver_index,
                       device_index,
                       properties.modelName,
                       properties.vendorName,
                       properties.driverVersion);
            else
                printf("Device %u:%u\n", driver_index, device_index);

            result = zesDeviceEnumFans(devices[device_index], &fan_count, NULL);
            if (result != ZE_RESULT_SUCCESS) {
                print_result("fan enumeration", result);
                continue;
            }
            printf("  Fan handles: %u\n", fan_count);

            fans = calloc(fan_count, sizeof(*fans));
            if (fan_count && !fans) {
                perror("calloc");
                free(devices);
                free(drivers);
                return EXIT_FAILURE;
            }

            result = zesDeviceEnumFans(devices[device_index], &fan_count, fans);
            if (result == ZE_RESULT_SUCCESS) {
                for (uint32_t fan_index = 0; fan_index < fan_count; ++fan_index)
                    inspect_fan(fans[fan_index], fan_index);
            } else {
                print_result("fan handles", result);
            }
            free(fans);
        }
        free(devices);
    }

    free(drivers);
    return EXIT_SUCCESS;
}