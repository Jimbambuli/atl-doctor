// SPDX-License-Identifier: GPL-3.0-or-later
/* libatl_doctor_wheel.so: LD_PRELOAD shim for the app under test (record --headless/--steps sets it).
 *
 * KWin's org_kde_kwin_fake_input.axis reaches the client as a bare wl_pointer.axis (no source, no value120),
 * which GTK treats as precise surface-unit scrolling (touchpad). A real wheel notch arrives as
 * axis_source(wheel) + axis_value120(+-120) + axis(+-15) in one frame, and GTK then delivers unit WHEEL.
 * `wheel` steps of atl-doctor-input mark their axis value with +-(15 + 1/256); only those are rewritten into
 * real notches. Every other axis event (`wheel-smooth`, a real device) passes unchanged.
 *
 * ATL_DOCTOR_WHEEL_LOG=FILE appends one line "notch<TAB>AXIS<TAB>+1|-1" per rewritten notch.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wayland-client.h>

struct wrap {
	struct wl_pointer_listener l;
	const struct wl_pointer_listener *orig;
	void *data;
};

static void log_notch(uint32_t axis, int32_t sign)
{
	const char *path = getenv("ATL_DOCTOR_WHEEL_LOG");
	FILE *f = path ? fopen(path, "a") : NULL;

	if (!f)
		return;
	fprintf(f, "notch\t%" PRIu32 "\t%+d\n", axis, (int)sign);
	fclose(f);
}

static void my_axis(void *data, struct wl_pointer *p, uint32_t time, uint32_t axis, wl_fixed_t value)
{
	struct wrap *w = data;
	uint32_t v = wl_proxy_get_version((struct wl_proxy *)p);

	if ((((value < 0) ? -value : value) & 0xff) == 1) { /* marked: one real notch */
		int32_t sign = value > 0 ? 1 : -1;

		if (v >= 5 && w->orig->axis_source)
			w->orig->axis_source(w->data, p, WL_POINTER_AXIS_SOURCE_WHEEL);
		if (v >= 8 && w->orig->axis_value120)
			w->orig->axis_value120(w->data, p, axis, sign * 120);
		else if (v >= 5 && w->orig->axis_discrete)
			w->orig->axis_discrete(w->data, p, axis, sign);
		value = wl_fixed_from_int(sign * 15);
		log_notch(axis, sign);
	}
	w->orig->axis(w->data, p, time, axis, value);
}

#define FWD(name, sig, args) static void my_##name sig { struct wrap *w = data; if (w->orig->name) w->orig->name args; }
FWD(enter, (void *data, struct wl_pointer *p, uint32_t s, struct wl_surface *su, wl_fixed_t x, wl_fixed_t y), (w->data, p, s, su, x, y))
FWD(leave, (void *data, struct wl_pointer *p, uint32_t s, struct wl_surface *su), (w->data, p, s, su))
FWD(motion, (void *data, struct wl_pointer *p, uint32_t t, wl_fixed_t x, wl_fixed_t y), (w->data, p, t, x, y))
FWD(button, (void *data, struct wl_pointer *p, uint32_t s, uint32_t t, uint32_t b, uint32_t st), (w->data, p, s, t, b, st))
FWD(frame, (void *data, struct wl_pointer *p), (w->data, p))
FWD(axis_source, (void *data, struct wl_pointer *p, uint32_t s), (w->data, p, s))
FWD(axis_stop, (void *data, struct wl_pointer *p, uint32_t t, uint32_t a), (w->data, p, t, a))
FWD(axis_discrete, (void *data, struct wl_pointer *p, uint32_t a, int32_t d), (w->data, p, a, d))
FWD(axis_value120, (void *data, struct wl_pointer *p, uint32_t a, int32_t d), (w->data, p, a, d))
FWD(axis_relative_direction, (void *data, struct wl_pointer *p, uint32_t a, uint32_t d), (w->data, p, a, d))

int wl_proxy_add_listener(struct wl_proxy *proxy, void (**impl)(void), void *data)
{
	static int (*real)(struct wl_proxy *, void (**)(void), void *);

	if (!real)
		*(void **)&real = dlsym(RTLD_NEXT, "wl_proxy_add_listener");
	if (strcmp(wl_proxy_get_class(proxy), "wl_pointer") == 0) {
		struct wrap *w = calloc(1, sizeof *w);

		if (!w)
			return real(proxy, impl, data);
		w->orig = (const struct wl_pointer_listener *)impl;
		w->data = data;
		w->l = (struct wl_pointer_listener){ my_enter, my_leave, my_motion, my_button, my_axis, my_frame,
			my_axis_source, my_axis_stop, my_axis_discrete, my_axis_value120, my_axis_relative_direction };
		return real(proxy, (void (**)(void))&w->l, w);
	}
	return real(proxy, impl, data);
}
