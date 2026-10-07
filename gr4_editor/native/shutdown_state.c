// Copyright (C) 2026 DoYitNow
// SPDX-License-Identifier: GPL-2.0-only

/* Global End Screen choice in its own eight-byte APData record. No native
 * UserData, RAW owner or ten UserMode snapshots receive additional fields. */
#include <stdint.h>

typedef struct { uint32_t magic, choice; } ShutdownState;
#define STATE_MAGIC 0x32424753u /* SGB2: stable selection ID, never list position */
#define LEGACY_STATE_MAGIC 0x31424753u /* SGB1: two old positional slots */
typedef struct { uint32_t choice, resource; } ShutdownChoice;
static const ShutdownChoice choices[] = SHUTDOWN_CHOICES;
#define APP_GET ((void *(*)(void))0x5323EA84u)
#define DISPLAY_GET ((void *(*)(void *))0x5323ED78u)
#define JPEG_REQUEST ((uint32_t (*)(void *, uint32_t))0x53262558u)

static uint32_t allowed(uint32_t choice) {
    for (uint32_t i = 0; i < SHUTDOWN_CHOICE_COUNT; ++i)
        if (choices[i].choice == choice) return 1;
    return 0;
}

static uint32_t normalize(ShutdownState record) {
    uint32_t choice = record.choice;
    if (record.magic == LEGACY_STATE_MAGIC) {
        if (choice == 1) choice = SHUTDOWN_LEGACY_1;
        else if (choice == 2) choice = SHUTDOWN_LEGACY_2;
        else return 0;
    } else if (record.magic != STATE_MAGIC) return 0;
    return allowed(choice) ? choice : 0;
}

static void *apdata(void) {
    uint8_t *factory = ((uint8_t *(*)(void))0x53A81480u)();
    return factory ? *(void **)(factory + 8) : 0;
}

static uint32_t read_state(ShutdownState *record) {
    void *ap = apdata();
    if (!ap || !((uint32_t (*)(void *, uint32_t))0x53A7E2B8u)(ap, SHUTDOWN_RECORD)) return 0;
    if (((uint32_t (*)(void *, uint32_t))0x53A7E208u)(ap, SHUTDOWN_RECORD) != sizeof(ShutdownState)) return 0;
    return ((uint32_t (*)(void *, uint32_t, uint32_t, void *))0x53A7E434u)
            (ap, SHUTDOWN_RECORD, sizeof(*record), record);
}

uint32_t shutdown_get(void) {
    ShutdownState record = { 0, 0 };
    return read_state(&record) ? normalize(record) : 0;
}

static uint32_t write_choice(uint32_t choice) {
    void *ap = apdata();
    if (!ap) return 0;
    ShutdownState record = { STATE_MAGIC, choice };
    return ((uint32_t (*)(void *, uint32_t, uint32_t, const void *))
            0x53A7E518u)(ap, SHUTDOWN_RECORD, sizeof(record), &record);
}

/* Confirmation writes only the demo record. Each lookup validates exact
 * length and magic with native Domain APIs; no spare fixed RAM is assumed. */
uint32_t shutdown_set(uint32_t choice) {
    if (!allowed(choice) || shutdown_get() == choice) return 0;
    return write_choice(choice) ? 1 : 0;
}

uint32_t shutdown_save_global(void) {
    ShutdownState record = { 0, 0 };
    uint32_t loaded = read_state(&record);
    if (loaded && record.magic == STATE_MAGIC && allowed(record.choice)) return 1;
    return write_choice(loaded ? normalize(record) : 0);
}

void shutdown_load_global(void) {
    (void)shutdown_get();
}

extern uint32_t prior_shutdown_save(void *mode);
extern uint32_t prior_shutdown_load(void *mode, void *current, void *a2, void *a3);

uint32_t shutdown_state_save(void *mode) {
    uint32_t result = prior_shutdown_save(mode);
    shutdown_save_global();
    return result;
}

uint32_t shutdown_state_load(void *mode, void *current, void *a2, void *a3) {
    uint32_t result = prior_shutdown_load(mode, current, a2, a3);
    shutdown_load_global();
    return result;
}

/* Recreate only the displaced frame-input instruction. Everything behind the
 * original selector remains byte-identical, including edition/variant logic. */
__attribute__((naked)) static uint32_t original_background(void *controller) {
    __asm__ volatile("mov ip, sp\n b original_shutdown_background_body");
}

uint32_t shutdown_request_background(void *controller) {
    uint32_t selected = shutdown_get();
    if (!selected) return original_background(controller);
    for (uint32_t i = 1; i < SHUTDOWN_CHOICE_COUNT; ++i)
        if (choices[i].choice == selected)
            return JPEG_REQUEST(DISPLAY_GET(APP_GET()), choices[i].resource);
    return original_background(controller);
}
