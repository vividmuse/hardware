/*
 * frame-count: a compositor's output frames over a period, seen by a Wayland
 * client, for comparing Labwc's renderers (pixman and the GPU) the same way.
 *
 *   WAYLAND_DISPLAY=... frame-count SECONDS
 *
 * It maps a 32x32 window and asks for presentation feedback
 * (wp_presentation) on each commit; a commit carries no new content, so it
 * damages nothing. The compositor answers "presented" once a frame it put
 * on the output drew the window again, with that frame's time.
 * - wlroots draws a surface again only where a frame's damage meets it, and
 *   the damage of a window below stops where an opaque one covers it. So the
 *   window is half transparent, and has to lie over what changes (the
 *   runner's scrolling foot; Labwc centres both).
 * - Without vblank (simpledrm) Labwc starts the next frame as soon as one is
 *   out, often before a client could answer "presented" with a commit. So
 *   the client commits every COMMIT_MS whatever it heard: each frame takes
 *   the latest commit's feedback, and the ones before it are discarded.
 * - Frame callbacks would not do: a pending one makes wlroots schedule a
 *   frame, and without vblank they would spin with nothing rendered.
 * On the GPU the runner compares the count with the G9P trace's frames.
 *
 * Prints one JSON line: presented frames, discarded feedback, and the
 * intervals between presented frames (as run-pvr-t8011.py's frame_stats).
 */
#define _GNU_SOURCE
#include <errno.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#include <wayland-client.h>
#include "presentation-time-client-protocol.h"
#include "xdg-shell-client-protocol.h"

#define SIZE 32
#define MAX_FRAMES (1 << 20)
#define COMMIT_MS 2

static struct wl_compositor *compositor;
static struct wl_shm *shm;
static struct xdg_wm_base *wm_base;
static struct wp_presentation *presentation;
static struct wl_surface *surface;
static int configured, closed;
static double *times;
static unsigned frames, discarded;

static double now_ms(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec * 1e3 + ts.tv_nsec / 1e6;
}

static void global(void *data, struct wl_registry *registry, uint32_t name,
		   const char *interface, uint32_t version)
{
	if (!strcmp(interface, wl_compositor_interface.name))
		compositor = wl_registry_bind(registry, name, &wl_compositor_interface, 1);
	else if (!strcmp(interface, wl_shm_interface.name))
		shm = wl_registry_bind(registry, name, &wl_shm_interface, 1);
	else if (!strcmp(interface, xdg_wm_base_interface.name))
		wm_base = wl_registry_bind(registry, name, &xdg_wm_base_interface, 1);
	else if (!strcmp(interface, wp_presentation_interface.name))
		presentation = wl_registry_bind(registry, name, &wp_presentation_interface, 1);
}

static void global_remove(void *data, struct wl_registry *registry, uint32_t name)
{
}

static const struct wl_registry_listener registry_listener = { global, global_remove };

static void ping(void *data, struct xdg_wm_base *base, uint32_t serial)
{
	xdg_wm_base_pong(base, serial);
}

static const struct xdg_wm_base_listener wm_base_listener = { ping };

static void surface_configure(void *data, struct xdg_surface *xdg_surface, uint32_t serial)
{
	xdg_surface_ack_configure(xdg_surface, serial);
	configured = 1;
}

static const struct xdg_surface_listener xdg_surface_listener = { surface_configure };

static void toplevel_configure(void *data, struct xdg_toplevel *toplevel, int32_t width,
			       int32_t height, struct wl_array *states)
{
}

static void toplevel_close(void *data, struct xdg_toplevel *toplevel)
{
	closed = 1;
}

static const struct xdg_toplevel_listener toplevel_listener = { toplevel_configure,
								toplevel_close };

static void sync_output(void *data, struct wp_presentation_feedback *feedback,
			struct wl_output *output)
{
}

static void presented(void *data, struct wp_presentation_feedback *feedback, uint32_t tv_sec_hi,
		      uint32_t tv_sec_lo, uint32_t tv_nsec, uint32_t refresh, uint32_t seq_hi,
		      uint32_t seq_lo, uint32_t flags)
{
	const double t = (((uint64_t)tv_sec_hi << 32) | tv_sec_lo) * 1e3 + tv_nsec / 1e6;

	if (frames < MAX_FRAMES)
		times[frames++] = t;
	wp_presentation_feedback_destroy(feedback);
}

static void feedback_discarded(void *data, struct wp_presentation_feedback *feedback)
{
	discarded++;
	wp_presentation_feedback_destroy(feedback);
}

static const struct wp_presentation_feedback_listener feedback_listener = {
	sync_output, presented, feedback_discarded
};

static void request(void)
{
	struct wp_presentation_feedback *feedback = wp_presentation_feedback(presentation, surface);

	wp_presentation_feedback_add_listener(feedback, &feedback_listener, NULL);
	wl_surface_commit(surface);
}

static struct wl_buffer *grey_buffer(void)
{
	const int stride = SIZE * 4, size = stride * SIZE;
	int fd = memfd_create("frame-count", MFD_CLOEXEC);
	uint32_t *pixels;
	struct wl_shm_pool *pool;
	struct wl_buffer *buffer;

	if (fd < 0 || ftruncate(fd, size) < 0)
		return NULL;
	pixels = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
	if (pixels == MAP_FAILED)
		return NULL;
	for (int i = 0; i < SIZE * SIZE; i++)
		pixels[i] = 0x80808080;	/* premultiplied, half transparent */
	munmap(pixels, size);
	pool = wl_shm_create_pool(shm, fd, size);
	buffer = wl_shm_pool_create_buffer(pool, 0, SIZE, SIZE, stride, WL_SHM_FORMAT_ARGB8888);
	wl_shm_pool_destroy(pool);
	close(fd);
	return buffer;
}

/* one round of events, waiting at most timeout_ms */
static int pump(struct wl_display *display, int timeout_ms)
{
	struct pollfd p = { wl_display_get_fd(display), POLLIN, 0 };

	while (wl_display_prepare_read(display) != 0)
		if (wl_display_dispatch_pending(display) < 0)
			return -1;
	wl_display_flush(display);
	if (poll(&p, 1, timeout_ms) > 0) {
		if (wl_display_read_events(display) < 0)
			return -1;
	} else {
		wl_display_cancel_read(display);
	}
	return wl_display_dispatch_pending(display);
}

int main(int argc, char **argv)
{
	const double seconds = argc > 1 ? atof(argv[1]) : 0;
	struct wl_display *display;
	struct wl_registry *registry;
	struct xdg_surface *xdg_surface;
	struct xdg_toplevel *toplevel;
	struct wl_buffer *buffer;
	double end;

	if (seconds <= 0 || seconds > 3600) {
		fprintf(stderr, "usage: frame-count SECONDS\n");
		return 2;
	}
	times = calloc(MAX_FRAMES, sizeof(*times));
	display = wl_display_connect(NULL);
	if (!times || !display) {
		fprintf(stderr, "frame-count: no Wayland display\n");
		return 1;
	}
	registry = wl_display_get_registry(display);
	wl_registry_add_listener(registry, &registry_listener, NULL);
	wl_display_roundtrip(display);
	if (!compositor || !shm || !wm_base || !presentation) {
		fprintf(stderr, "frame-count: the compositor lacks %s\n",
			!presentation ? "wp_presentation" : "a core global");
		return 1;
	}
	xdg_wm_base_add_listener(wm_base, &wm_base_listener, NULL);
	surface = wl_compositor_create_surface(compositor);
	xdg_surface = xdg_wm_base_get_xdg_surface(wm_base, surface);
	xdg_surface_add_listener(xdg_surface, &xdg_surface_listener, NULL);
	toplevel = xdg_surface_get_toplevel(xdg_surface);
	xdg_toplevel_add_listener(toplevel, &toplevel_listener, NULL);
	xdg_toplevel_set_title(toplevel, "frame-count");
	xdg_toplevel_set_app_id(toplevel, "j208-frame-count");
	wl_surface_commit(surface);
	while (!configured && !closed)
		if (wl_display_dispatch(display) < 0)
			return 1;
	buffer = grey_buffer();
	if (!buffer) {
		fprintf(stderr, "frame-count: no shm buffer: %s\n", strerror(errno));
		return 1;
	}
	wl_surface_attach(surface, buffer, 0, 0);
	wl_surface_damage(surface, 0, 0, SIZE, SIZE);
	request();

	double last = now_ms();

	end = last + seconds * 1e3;
	while (!closed && now_ms() < end) {
		if (pump(display, 1) < 0)
			break;
		if (now_ms() - last >= COMMIT_MS) {
			request();
			last = now_ms();
		}
	}

	/* the first frame shows the window itself; it is not counted */
	unsigned intervals = 0, under_20 = 0, from_20_to_35 = 0, over_35 = 0;
	double sum = 0;

	for (unsigned i = 2; i < frames; i++) {
		const double gap = times[i] - times[i - 1];

		intervals++;
		sum += gap;
		under_20 += gap < 20;
		from_20_to_35 += gap >= 20 && gap < 35;
		over_35 += gap >= 35;
	}
	printf("{\"seconds\": %.1f, \"presented\": %u, \"discarded\": %u, \"intervals\": %u, "
	       "\"mean_ms\": %.2f, \"under_20_ms\": %u, \"from_20_to_35_ms\": %u, "
	       "\"over_35_ms\": %u, \"fps\": %.1f}\n",
	       seconds, frames, discarded, intervals, intervals ? sum / intervals : 0, under_20,
	       from_20_to_35, over_35,
	       intervals ? 1e3 * intervals / (times[frames - 1] - times[1]) : 0);
	wl_display_disconnect(display);
	return 0;
}
