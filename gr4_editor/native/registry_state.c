/* Compiled at the appended RTOS address. SLOT_ROWS and SLOT_COUNT are injected
 * by the known .27 backend; original UserData snapshots keep their own layout.
 */
#include <stdint.h>
typedef struct { uint32_t main_offset, mirror_offset, shadow_offset, record, style; uint8_t defaults[36]; } Slot;
static const Slot slots[SLOT_COUNT] = { SLOT_ROWS };
typedef void *(*copy_fn)(void *, const void *, uint32_t);
typedef void *(*fill_fn)(void *, int, uint32_t);
#define COPY ((copy_fn)0x53156140u)
#define FILL ((fill_fn)0x53156768u)
#define NEW ((uint8_t *(*)(uint32_t))0x538E09B0u)

static void initialize(uint8_t *p, const Slot *s) {
    for (unsigned i = 0; i < 11; ++i) COPY(p + i * 36, s->defaults, 36);
}

static uint8_t *owner(void) {
    uint8_t *p = *(uint8_t *volatile *)0x550885B4u;
    if (!p) p = ((uint8_t *(*)(void))0x5323F078u)();
    return p;
}

static uint8_t *ensure(const Slot *s) {
    uint8_t *p = owner();
    return p ? p + s->shadow_offset : 0;
}

static uint8_t *main_of(const Slot *s) { return owner() + s->main_offset; }
static uint8_t *mirror_of(const Slot *s) { return owner() + s->mirror_offset; }

uint8_t *registry_main(uint32_t style) {
    for (unsigned i = 0; i < SLOT_COUNT; ++i)
        if (slots[i].style == style) return main_of(&slots[i]);
    return 0;
}

/* The existing UserData bulk path reads these four photo preset selections
 * at +0xc3f; video selections use another field and remain unchanged. */
void registry_normalize_selection(void *userdata) {
    uint8_t *p = userdata;
    for (unsigned group = 0; group < 4; ++group) {
        uint8_t style = p[0xc3f + group];
        if (style < 34) continue;
        unsigned active = 0;
        for (unsigned i = 0; i < SLOT_COUNT; ++i)
            if (slots[i].style == style) active = 1;
        if (!active) p[0xc3f + group] = 11;
    }
}

static void normalize_current(void) {
    registry_normalize_selection(((void *(*)(void))0x5323F018u)());
}

void registry_initialize_owner(uint8_t *p) {
    for (unsigned i = 0; i < SLOT_COUNT; ++i) {
        const Slot *s = &slots[i];
        COPY(p + s->main_offset, s->defaults, 36);
        COPY(p + s->mirror_offset, s->defaults, 36);
        initialize(p + s->shadow_offset, s);
    }
}

static int slot_of(const uint8_t *mode, const void *snapshot) {
    const uint32_t *list = (const uint32_t *)(mode + 4);
    for (int i = 0; i < 10; ++i) if (list[i] == (uint32_t)snapshot) return i;
    return -1;
}

void registry_slot_save(const uint8_t *mode, const void *snapshot) {
    int index = slot_of(mode, snapshot);
    if (index < 0) return;
    for (unsigned i = 0; i < SLOT_COUNT; ++i) {
        uint8_t *p = ensure(&slots[i]);
        if (p) COPY(p + 36 + index * 36, main_of(&slots[i]), 36);
    }
}

void registry_slot_restore(const uint8_t *mode, const void *snapshot) {
    int index = slot_of(mode, snapshot);
    if (index < 0) return;
    for (unsigned i = 0; i < SLOT_COUNT; ++i) {
        const Slot *s = &slots[i]; uint8_t *p = ensure(s);
        if (p) { COPY(main_of(s), p + 36 + index * 36, 36); COPY(mirror_of(s), main_of(s), 36); }
    }
    normalize_current();
}

void registry_slot_reset(const uint8_t *mode, const void *snapshot) {
    int index = slot_of(mode, snapshot);
    if (index < 0) return;
    for (unsigned i = 0; i < SLOT_COUNT; ++i) {
        uint8_t *p = ensure(&slots[i]);
        if (p) COPY(p + 36 + index * 36, slots[i].defaults, 36);
    }
}

void *registry_slot_copy(void *dst, const void *src, uint32_t n, const uint8_t *mode) {
    COPY(dst, src, n);
    int a = slot_of(mode, dst), b = slot_of(mode, src);
    if (a >= 0 && b >= 0) for (unsigned i = 0; i < SLOT_COUNT; ++i) {
        uint8_t *p = ensure(&slots[i]);
        if (p) COPY(p + 36 + a * 36, p + 36 + b * 36, 36);
    }
    return dst;
}

static void *apdata(void) {
    return *(void **)(((uint8_t *(*)(void))0x53A81480u)() + 8);
}

/* The skipped native entry instruction establishes the frame pointer input.
 * A normal C call directly into +4 would leave ip unrelated to the stack.
 * The linker binds these body symbols to the original, unmodified methods.
 */
__attribute__((naked)) static uint32_t original_save(void *userdata) {
    __asm__ volatile("mov ip, sp\n b original_save_body");
}
__attribute__((naked)) static uint32_t original_load(void *userdata, uint32_t arg1, uint32_t arg2, uint32_t arg3) {
    __asm__ volatile("mov ip, sp\n b original_load_body");
}

uint32_t registry_save(void *userdata) {
    normalize_current();
    uint32_t result = original_save(userdata);
    void *ap = apdata();
    for (unsigned i = 0; i < SLOT_COUNT; ++i) {
        uint8_t *p = ensure(&slots[i]);
        if (p) {
            COPY(p, main_of(&slots[i]), 36);
            ((uint32_t (*)(void *, uint32_t, uint32_t, const void *))0x53A7E518u)(ap, slots[i].record, 396, p);
        }
    }
    return result;
}

uint32_t registry_load(void *userdata, uint32_t arg1, uint32_t arg2, uint32_t arg3) {
    uint32_t result = original_load(userdata, arg1, arg2, arg3);
    normalize_current();
    void *ap = apdata();
    for (unsigned i = 0; i < SLOT_COUNT; ++i) {
        const Slot *s = &slots[i]; uint8_t *p = ensure(s);
        COPY(main_of(s), s->defaults, 36); COPY(mirror_of(s), s->defaults, 36);
        if (!p) continue;
        initialize(p, s);
        if (!((uint32_t (*)(void *, uint32_t))0x53A7E2B8u)(ap, s->record)) continue;
        uint32_t n = ((uint32_t (*)(void *, uint32_t))0x53A7E208u)(ap, s->record);
        if (n != 36 && n != 396) continue;
        if (((uint32_t (*)(void *, uint32_t, uint32_t, void *))0x53A7E434u)(ap, s->record, n, p)) {
            COPY(main_of(s), p, 36); COPY(mirror_of(s), p, 36);
        }
    }
    return result;
}
