// SPDX-License-Identifier: GPL-3.0-or-later
/* atl-doctor-input: drive a KWin session through org_kde_kwin_fake_input.
 *
 *   atl-doctor-input serve FIFO STATUS
 *
 * Stays connected (the fake pointer exists only while a client is) and reads lines from FIFO:
 *   click X Y                    left click
 *   long X Y MS                  press, hold MS, release
 *   drag X1 Y1 X2 Y2 [HOLD_MS]   press, hold (default 300 ms), move in ten steps, release
 *   move X Y                     pointer motion
 *   wheel X Y N [MS]             N real wheel notches (N > 0 down), MS between notches (default 50); the axis
 *                                value is marked, libatl_doctor_wheel.so in the app turns it into source wheel +
 *                                value120 (without the shim GTK sees smooth scrolling)
 *   wheel-smooth X Y DY [MS]     touchpad-like smooth scrolling: axis events of at most 15 units until DY units
 *                                (DY > 0 down) are sent, MS between them (default 50)
 *   key NAME[+NAME...]           xkb keysym names and ctrl/shift/alt/super; pressed in order,
 *                                released in reverse ("key ctrl+a")
 *   text STRING                  UTF-8, typed as evdev keycodes from the compositor's keymap
 *   ping N                       answers "pong N" in STATUS once every earlier line is processed
 *   quit
 * STATUS (append): "pong N", and "ERROR ..." for unknown lines, keys or characters not in the keymap
 * and protocol errors. After a protocol error the helper reconnects instead of carrying on.
 * Keys are sent as evdev keycodes: keyboard_keysym exists only from fake_input version 6, and older
 * KWin versions end the connection on it.
 */
#define _POSIX_C_SOURCE 200809L
#include <inttypes.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>
#include <wayland-client.h>
#include <xkbcommon/xkbcommon.h>
#include "fake-input-client-protocol.h"

#define BTN_LEFT 0x110
#define KEY_LEFTCTRL 29
#define KEY_LEFTSHIFT 42
#define KEY_LEFTALT 56
#define KEY_RIGHTALT 100 /* AltGr (level 3) */
#define KEY_LEFTMETA 125
#define MAXCP 0x3000
#define MAXKEYS 8

static struct org_kde_kwin_fake_input *fi;
static struct wl_display *dpy;
static struct wl_seat *seat;
static struct xkb_context *xctx;
static struct xkb_keymap *keymap;
static FILE *status;

/* codepoint -> evdev keycode + modifiers (code 0 = not typeable) */
static struct typed { uint16_t code; uint8_t shift, altgr; } tab[MAXCP];

static void report(const char *fmt, const char *s, uint32_t n)
{
	fprintf(stderr, fmt, s, n);
	if (status) {
		fprintf(status, fmt, s, n);
		fflush(status);
	}
}

static void kb_keymap(void *d, struct wl_keyboard *k, uint32_t fmt, int32_t fd, uint32_t size)
{
	(void)d;
	(void)k;
	char *m = mmap(NULL, size, PROT_READ, MAP_PRIVATE, fd, 0);
	if (m != MAP_FAILED && fmt == WL_KEYBOARD_KEYMAP_FORMAT_XKB_V1) {
		if (keymap)
			xkb_keymap_unref(keymap);
		keymap = xkb_keymap_new_from_string(xctx, m, XKB_KEYMAP_FORMAT_TEXT_V1, 0);
	}
	if (m != MAP_FAILED)
		munmap(m, size);
	close(fd);
}
static void kb_enter(void *d, struct wl_keyboard *k, uint32_t s, struct wl_surface *f, struct wl_array *a)
{
	(void)d, (void)k, (void)s, (void)f, (void)a;
}
static void kb_leave(void *d, struct wl_keyboard *k, uint32_t s, struct wl_surface *f)
{
	(void)d, (void)k, (void)s, (void)f;
}
static void kb_key(void *d, struct wl_keyboard *k, uint32_t s, uint32_t t, uint32_t key, uint32_t st)
{
	(void)d, (void)k, (void)s, (void)t, (void)key, (void)st;
}
static void kb_mods(void *d, struct wl_keyboard *k, uint32_t s, uint32_t a, uint32_t b, uint32_t c, uint32_t g)
{
	(void)d, (void)k, (void)s, (void)a, (void)b, (void)c, (void)g;
}
static void kb_repeat(void *d, struct wl_keyboard *k, int32_t r, int32_t dl)
{
	(void)d, (void)k, (void)r, (void)dl;
}
static const struct wl_keyboard_listener kl = {kb_keymap, kb_enter, kb_leave, kb_key, kb_mods, kb_repeat};

static void reg_global(void *d, struct wl_registry *r, uint32_t name, const char *iface, uint32_t ver)
{
	(void)d;
	if (!strcmp(iface, "org_kde_kwin_fake_input"))
		fi = wl_registry_bind(r, name, &org_kde_kwin_fake_input_interface, ver < 6 ? ver : 6);
	else if (!strcmp(iface, "wl_seat") && !seat)
		seat = wl_registry_bind(r, name, &wl_seat_interface, 1);
}
static void reg_remove(void *d, struct wl_registry *r, uint32_t name)
{
	(void)d, (void)r, (void)name;
}
static const struct wl_registry_listener rl = {reg_global, reg_remove};

/* the key and level producing keysym with the fewest modifiers; 0 if none */
static uint16_t lookup(xkb_keysym_t want, uint8_t *shift, uint8_t *altgr)
{
	xkb_mod_index_t ishift = xkb_keymap_mod_get_index(keymap, XKB_MOD_NAME_SHIFT);
	xkb_mod_index_t i3 = xkb_keymap_mod_get_index(keymap, "Mod5");
	uint16_t best = 0;
	uint8_t bs = 0, ba = 0;
	for (xkb_keycode_t kc = xkb_keymap_min_keycode(keymap); kc <= xkb_keymap_max_keycode(keymap) && kc < 264; kc++) {
		xkb_level_index_t nl = xkb_keymap_num_levels_for_key(keymap, kc, 0);
		for (xkb_level_index_t l = 0; l < nl; l++) {
			const xkb_keysym_t *syms;
			xkb_mod_mask_t masks[8];
			if (xkb_keymap_key_get_syms_by_level(keymap, kc, 0, l, &syms) != 1)
				continue;
			if (syms[0] != want)
				continue;
			size_t nm = xkb_keymap_key_get_mods_for_level(keymap, kc, 0, l, masks, 8);
			for (size_t i = 0; i < nm; i++) {
				xkb_mod_mask_t m = masks[i];
				uint8_t sh = (m >> ishift) & 1, ag = i3 != XKB_MOD_INVALID && ((m >> i3) & 1);
				m &= ~((1u << ishift) | (i3 != XKB_MOD_INVALID ? 1u << i3 : 0));
				if (m) /* needs Ctrl/Alt/...: not usable */
					continue;
				if (!best || sh + ag < bs + ba) {
					best = (uint16_t)(kc - 8);
					bs = sh;
					ba = ag;
				}
			}
		}
	}
	*shift = bs;
	*altgr = ba;
	return best;
}

/* tab[] in one pass over the keymap: per character the key/level with the fewest modifiers */
static void fill_table(void)
{
	xkb_mod_index_t ishift = xkb_keymap_mod_get_index(keymap, XKB_MOD_NAME_SHIFT);
	xkb_mod_index_t i3 = xkb_keymap_mod_get_index(keymap, "Mod5");
	memset(tab, 0, sizeof tab);
	for (xkb_keycode_t kc = xkb_keymap_min_keycode(keymap); kc <= xkb_keymap_max_keycode(keymap) && kc < 264; kc++) {
		xkb_level_index_t nl = xkb_keymap_num_levels_for_key(keymap, kc, 0);
		for (xkb_level_index_t l = 0; l < nl; l++) {
			const xkb_keysym_t *syms;
			xkb_mod_mask_t masks[8];
			if (xkb_keymap_key_get_syms_by_level(keymap, kc, 0, l, &syms) != 1)
				continue;
			uint32_t cp = xkb_keysym_to_utf32(syms[0]);
			size_t nm = xkb_keymap_key_get_mods_for_level(keymap, kc, 0, l, masks, 8);
			if (cp < 0x20 || cp >= MAXCP)
				continue;
			for (size_t i = 0; i < nm; i++) {
				xkb_mod_mask_t m = masks[i];
				uint8_t sh = (m >> ishift) & 1, ag = i3 != XKB_MOD_INVALID && ((m >> i3) & 1);
				m &= ~((1u << ishift) | (i3 != XKB_MOD_INVALID ? 1u << i3 : 0));
				if (m)
					continue;
				struct typed *t = &tab[cp];
				if (!t->code || sh + ag < t->shift + t->altgr) {
					t->code = (uint16_t)(kc - 8);
					t->shift = sh;
					t->altgr = ag;
				}
			}
		}
	}
}

static int32_t connect_display(void)
{
	fi = NULL;
	seat = NULL;
	dpy = wl_display_connect(NULL);
	if (!dpy)
		return -1;
	struct wl_registry *reg = wl_display_get_registry(dpy);
	wl_registry_add_listener(reg, &rl, NULL);
	wl_display_roundtrip(dpy);
	if (!fi)
		return -1;
	org_kde_kwin_fake_input_authenticate(fi, "atl-doctor", "test input");
	if (seat)
		wl_keyboard_add_listener(wl_seat_get_keyboard(seat), &kl, NULL);
	wl_display_roundtrip(dpy);
	wl_display_roundtrip(dpy);
	if (!keymap) { /* no keyboard on the seat: compile from XKB_DEFAULT_* */
		keymap = xkb_keymap_new_from_names(xctx, NULL, 0);
		report("atl-doctor-input: no keymap from the compositor, using XKB_DEFAULT_LAYOUT=%s%.0u\n",
		       getenv("XKB_DEFAULT_LAYOUT") ? getenv("XKB_DEFAULT_LAYOUT") : "us", 0);
	}
	if (!keymap)
		return -1;
	fill_table();
	fprintf(stderr, "atl-doctor-input: fake_input v%" PRIu32 ", keymap %s\n",
		wl_proxy_get_version((struct wl_proxy *)fi), xkb_keymap_layout_get_name(keymap, 0));
	return 0;
}

static void flush_ms(uint32_t ms)
{
	struct timespec ts = {.tv_sec = ms / 1000, .tv_nsec = (int64_t)(ms % 1000) * 1000000};
	wl_display_roundtrip(dpy);
	nanosleep(&ts, NULL);
}

static void move(int32_t x, int32_t y)
{
	org_kde_kwin_fake_input_pointer_motion_absolute(fi, wl_fixed_from_int(x), wl_fixed_from_int(y));
	flush_ms(30);
}

static void key(uint32_t code, uint32_t down)
{
	org_kde_kwin_fake_input_keyboard_key(fi, code, down);
}

static void type_cp(uint32_t cp)
{
	struct typed t = cp < MAXCP ? tab[cp] : (struct typed){0, 0, 0};
	if (!t.code) {
		report("ERROR text: character U+%s%04" PRIX32 " is not in the keymap\n", "", cp);
		return;
	}
	if (t.shift)
		key(KEY_LEFTSHIFT, 1);
	if (t.altgr)
		key(KEY_RIGHTALT, 1);
	key(t.code, 1);
	flush_ms(20);
	key(t.code, 0);
	if (t.altgr)
		key(KEY_RIGHTALT, 0);
	if (t.shift)
		key(KEY_LEFTSHIFT, 0);
	flush_ms(60);
}

/* "ctrl+shift+Tab": modifiers by name, everything else as xkb keysym name */
static void key_combo(char *combo)
{
	uint32_t codes[MAXKEYS * 2];
	uint8_t n = 0;
	for (char *name = strtok(combo, "+"); name; name = strtok(NULL, "+")) {
		if (n + 3 > MAXKEYS * 2) { /* one name gives up to three codes (Shift, AltGr, key) */
			report("ERROR key: too many keys in one combination%s%.0u\n", "", 0);
			return;
		}
		if (!strcmp(name, "ctrl")) {
			codes[n++] = KEY_LEFTCTRL;
		} else if (!strcmp(name, "shift")) {
			codes[n++] = KEY_LEFTSHIFT;
		} else if (!strcmp(name, "alt")) {
			codes[n++] = KEY_LEFTALT;
		} else if (!strcmp(name, "super")) {
			codes[n++] = KEY_LEFTMETA;
		} else {
			xkb_keysym_t sym = xkb_keysym_from_name(name, XKB_KEYSYM_NO_FLAGS);
			if (sym == XKB_KEY_NoSymbol)
				sym = xkb_keysym_from_name(name, XKB_KEYSYM_CASE_INSENSITIVE);
			uint8_t sh = 0, ag = 0;
			uint16_t code = sym == XKB_KEY_NoSymbol ? 0 : lookup(sym, &sh, &ag);
			if (!code) {
				report("ERROR key: '%s' is not in the keymap%.0u\n", name, 0);
				return;
			}
			if (sh)
				codes[n++] = KEY_LEFTSHIFT;
			if (ag)
				codes[n++] = KEY_RIGHTALT;
			codes[n++] = code;
		}
	}
	for (uint8_t i = 0; i < n; i++) {
		key(codes[i], 1);
		flush_ms(30);
	}
	flush_ms(50);
	for (uint8_t i = n; i > 0; i--)
		key(codes[i - 1], 0);
}

static void handle(char *line)
{
	int32_t a, b, c, d, n, hold = 300;
	char text[400];
	line[strcspn(line, "\n")] = 0;
	if (sscanf(line, "drag %" SCNd32 " %" SCNd32 " %" SCNd32 " %" SCNd32 " %" SCNd32, &a, &b, &c, &d, &hold) >= 4) {
		move(a, b);
		org_kde_kwin_fake_input_button(fi, BTN_LEFT, 1);
		flush_ms((uint32_t)hold);
		for (n = 1; n <= 10; n++)
			move(a + (c - a) * n / 10, b + (d - b) * n / 10);
		flush_ms(300);
		org_kde_kwin_fake_input_button(fi, BTN_LEFT, 0);
	} else if (sscanf(line, "long %" SCNd32 " %" SCNd32 " %" SCNd32, &a, &b, &c) == 3) {
		move(a, b);
		org_kde_kwin_fake_input_button(fi, BTN_LEFT, 1);
		flush_ms((uint32_t)c);
		org_kde_kwin_fake_input_button(fi, BTN_LEFT, 0);
	} else if (sscanf(line, "click %" SCNd32 " %" SCNd32, &a, &b) == 2) {
		move(a, b);
		flush_ms(70);
		org_kde_kwin_fake_input_button(fi, BTN_LEFT, 1);
		flush_ms(80);
		org_kde_kwin_fake_input_button(fi, BTN_LEFT, 0);
	} else if ((n = sscanf(line, "wheel-smooth %" SCNd32 " %" SCNd32 " %" SCNd32 " %" SCNd32, &a, &b, &c, &d)) >= 3) {
		int32_t left = abs(c);

		if (n == 3)
			d = 50;
		move(a, b);
		while (left > 0) {
			int32_t step = left < 15 ? left : 15;

			/* axis 0 = vertical; an unmarked value passes the shim unchanged */
			org_kde_kwin_fake_input_axis(fi, 0, c > 0 ? wl_fixed_from_int(step) : -wl_fixed_from_int(step));
			left -= step;
			flush_ms((uint32_t)d);
		}
	} else if ((n = sscanf(line, "wheel %" SCNd32 " %" SCNd32 " %" SCNd32 " %" SCNd32, &a, &b, &c, &d)) >= 3) {
		if (n == 3)
			d = 50;
		move(a, b);
		for (n = 0; n < abs(c); n++) {
			/* axis 0 = vertical; 15 + 1/256 marks one real notch for libatl_doctor_wheel.so */
			wl_fixed_t v = wl_fixed_from_int(15) + 1;

			org_kde_kwin_fake_input_axis(fi, 0, c > 0 ? v : -v);
			flush_ms((uint32_t)d);
		}
	} else if (sscanf(line, "move %" SCNd32 " %" SCNd32, &a, &b) == 2) {
		move(a, b);
	} else if (sscanf(line, "key %399s", text) == 1) {
		key_combo(text);
	} else if (!strncmp(line, "text ", 5)) {
		for (const uint8_t *p = (const uint8_t *)line + 5; *p; p++) { /* UTF-8 up to 3 bytes */
			uint32_t cp = *p;
			if ((cp & 0xe0) == 0xc0 && p[1]) {
				cp = ((cp & 0x1f) << 6) | (p[1] & 0x3f);
				p++;
			} else if ((cp & 0xf0) == 0xe0 && p[1] && p[2]) {
				cp = ((cp & 0x0f) << 12) | ((uint32_t)(p[1] & 0x3f) << 6) | (p[2] & 0x3f);
				p += 2;
			}
			type_cp(cp);
		}
	} else if (sscanf(line, "ping %" SCNd32, &a) == 1) {
		wl_display_roundtrip(dpy);
		if (!wl_display_get_error(dpy))
			report("pong %s%" PRIu32 "\n", "", (uint32_t)a);
		return;
	} else {
		report("ERROR unknown line: %s%.0u\n", line, 0);
		return;
	}
	wl_display_roundtrip(dpy);
}

int main(int argc, char **argv)
{
	if (argc != 4 || strcmp(argv[1], "serve")) {
		fprintf(stderr, "usage: atl-doctor-input serve FIFO STATUS\n");
		return 2;
	}
	status = fopen(argv[3], "a");
	xctx = xkb_context_new(0);
	if (!xctx || connect_display()) {
		report("ERROR %sno Wayland display, fake_input or keymap%.0u\n", "", 0);
		return 1;
	}
	for (;;) {
		FILE *f = fopen(argv[2], "r");
		if (!f)
			return 1;
		char line[512];
		while (fgets(line, sizeof line, f)) {
			if (!strncmp(line, "quit", 4)) {
				fclose(f);
				wl_display_disconnect(dpy);
				return 0;
			}
			handle(line);
			int32_t e = wl_display_get_error(dpy);
			if (e) { /* protocol error: report it, reconnect, keep serving */
				report("ERROR Wayland error %s(errno %" PRIu32 "), reconnecting\n", "", (uint32_t)e);
				wl_display_disconnect(dpy);
				if (connect_display()) {
					report("ERROR %sreconnect failed%.0u\n", "", 0);
					return 1;
				}
			}
		}
		fclose(f);
	}
}
