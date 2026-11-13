// SPDX-License-Identifier: MIT
/*
 * OpenGL ES through Zink on the J208's GPU, off screen: GBM on the GPU's
 * render node, EGL on GBM, a GLES 2 context, and five checks read back with
 * glReadPixels. It never opens the display device, so it runs next to the
 * running compositor.
 *
 *   MESA_LOADER_DRIVER_OVERRIDE=zink PVR_I_WANT_A_BROKEN_VULKAN_DRIVER=1 gl-probe
 *
 * clear     clear a 256x256 texture target, read back
 * triangle  one triangle, inside red, outside the clear colour
 * blend     source-over of 50% green over the clear colour
 * texture   a 2x2 texture sampled (nearest) over the whole target
 * dmabuf    what wlroots' GLES2 renderer does: a LINEAR XRGB8888 GBM buffer,
 *           exported as a dma-buf, imported as an EGLImage render buffer,
 *           cleared and drawn into, read back
 *
 * GLP_QUICK=1 or 2 runs one triangle only (see main). Prints one line per
 * check, then PASS or FAIL. GLP_NODE overrides the
 * render node (default: the first one whose driver is powervr).
 *
 * GLP_SCANOUT=WxH runs the compositor's allocation instead: wlroots creates
 * its GBM allocator on the display device (simpledrm), not on the render
 * node. A LINEAR XRGB8888 scanout buffer of the display's size from a GBM
 * device on the first DRM card that is not the GPU, imported into the
 * render node's EGL as above, the scenes drawn and the far corners checked.
 * Run it without MESA_LOADER_DRIVER_OVERRIDE=zink, as Labwc runs: Mesa
 * then pairs the display with Zink on the GPU (kmsro).
 *
 * GLP_UPLOADS=N runs N rounds of what a compositor does with a client's
 * buffer instead: a 64x64 texture filled anew with glTexSubImage2D, drawn
 * over the target and read back. Each round first fills and draws a decoy
 * colour, and fills the real one with that draw still pending, as a
 * compositor's next frame does: Zink copies straight into an idle texture
 * (VK_EXT_host_image_copy), but into a busy one through staging memory it
 * maps. A map the driver refused left the decoy (kept run 1).
 * GLP_UPLOAD_SIZE=S makes the texture SxS (64 by default).
 */
#define _GNU_SOURCE
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <GLES2/gl2ext.h>
#include <drm_fourcc.h>
#include <gbm.h>
#include <xf86drm.h>

#define W 256
#define H 256

static int failures;

static int open_node(void)
{
	const char *env = getenv("GLP_NODE");
	char path[64];

	if (env)
		return open(env, O_RDWR | O_CLOEXEC);
	for (int i = 128; i < 136; i++) {
		snprintf(path, sizeof(path), "/dev/dri/renderD%d", i);
		int fd = open(path, O_RDWR | O_CLOEXEC);
		if (fd < 0)
			continue;
		drmVersionPtr v = drmGetVersion(fd);
		int ok = v && !strcmp(v->name, "powervr");
		drmFreeVersion(v);
		if (ok) {
			printf("node: %s\n", path);
			return fd;
		}
		close(fd);
	}
	return -1;
}

static GLuint shader(GLenum type, const char *src)
{
	GLuint s = glCreateShader(type);
	GLint ok;

	glShaderSource(s, 1, &src, NULL);
	glCompileShader(s);
	glGetShaderiv(s, GL_COMPILE_STATUS, &ok);
	if (!ok) {
		char log[1024];
		glGetShaderInfoLog(s, sizeof(log), NULL, log);
		fprintf(stderr, "shader: %s\n", log);
		exit(1);
	}
	return s;
}

static GLuint program(const char *vs, const char *fs)
{
	GLuint p = glCreateProgram();
	GLint ok;

	glAttachShader(p, shader(GL_VERTEX_SHADER, vs));
	glAttachShader(p, shader(GL_FRAGMENT_SHADER, fs));
	glBindAttribLocation(p, 0, "pos");
	glLinkProgram(p);
	glGetProgramiv(p, GL_LINK_STATUS, &ok);
	if (!ok) {
		fprintf(stderr, "link failed\n");
		exit(1);
	}
	return p;
}

/* RGBA bytes at (x, y), y from the bottom as GL counts */
static unsigned int pixel(int x, int y)
{
	unsigned char p[4];

	glReadPixels(x, y, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, p);
	return (unsigned int)p[0] << 24 | p[1] << 16 | p[2] << 8 | p[3];
}

static int near(unsigned int a, unsigned int b)
{
	for (int s = 0; s < 32; s += 8) {
		int d = (int)(a >> s & 0xff) - (int)(b >> s & 0xff);
		if (d < -2 || d > 2)
			return 0;
	}
	return 1;
}

/* the first failing point fails the check */
static void check(const char *name, const int (*pts)[2], const unsigned int *want, int n,
		  unsigned int alpha_mask)
{
	for (int i = 0; i < n; i++) {
		unsigned int got = pixel(pts[i][0], pts[i][1]) | alpha_mask;
		if (!near(got, want[i] | alpha_mask)) {
			printf("%s: FAIL at (%d,%d) got 0x%08x want 0x%08x (glGetError 0x%x)\n", name,
			       pts[i][0], pts[i][1], got, want[i] | alpha_mask, glGetError());
			failures++;
			return;
		}
	}
	printf("%s: PASS\n", name);
}

/* the display: the first DRM card whose driver is not the GPU's */
static int open_kms(void)
{
	for (int i = 0; i < 4; i++) {
		char path[32];

		snprintf(path, sizeof(path), "/dev/dri/card%d", i);
		int fd = open(path, O_RDWR | O_CLOEXEC);
		if (fd < 0)
			continue;
		drmVersionPtr v = drmGetVersion(fd);
		int ok = v && strcmp(v->name, "powervr");
		if (ok)
			printf("display: %s (%s)\n", path, v->name);
		drmFreeVersion(v);
		if (ok)
			return fd;
		close(fd);
	}
	return -1;
}

/* a GBM buffer imported as an EGLImage render buffer, bound as the target */
static int bind_buffer(EGLDisplay dpy, struct gbm_bo *bo, const char *name)
{
	uint64_t linear = DRM_FORMAT_MOD_LINEAR;
	int dfd = gbm_bo_get_fd(bo);

	if (dfd < 0) {
		printf("%s: FAIL (gbm_bo_get_fd)\n", name);
		return 0;
	}
	EGLint attr[] = {
		EGL_WIDTH, (EGLint)gbm_bo_get_width(bo), EGL_HEIGHT, (EGLint)gbm_bo_get_height(bo),
		EGL_LINUX_DRM_FOURCC_EXT, DRM_FORMAT_XRGB8888,
		EGL_DMA_BUF_PLANE0_FD_EXT, dfd,
		EGL_DMA_BUF_PLANE0_OFFSET_EXT, (EGLint)gbm_bo_get_offset(bo, 0),
		EGL_DMA_BUF_PLANE0_PITCH_EXT, (EGLint)gbm_bo_get_stride(bo),
		EGL_DMA_BUF_PLANE0_MODIFIER_LO_EXT, (EGLint)(linear & 0xffffffff),
		EGL_DMA_BUF_PLANE0_MODIFIER_HI_EXT, (EGLint)(linear >> 32),
		EGL_NONE };
	PFNEGLCREATEIMAGEKHRPROC create_image = (void *)eglGetProcAddress("eglCreateImageKHR");
	PFNGLEGLIMAGETARGETRENDERBUFFERSTORAGEOESPROC rb_storage =
		(void *)eglGetProcAddress("glEGLImageTargetRenderbufferStorageOES");
	EGLImageKHR img = create_image(dpy, EGL_NO_CONTEXT, EGL_LINUX_DMA_BUF_EXT, NULL, attr);
	printf("%s: %ux%u, stride %u, modifier 0x%llx\n", name, gbm_bo_get_width(bo),
	       gbm_bo_get_height(bo), gbm_bo_get_stride(bo),
	       (unsigned long long)gbm_bo_get_modifier(bo));
	if (img == EGL_NO_IMAGE_KHR) {
		printf("%s: FAIL (eglCreateImageKHR 0x%x)\n", name, eglGetError());
		return 0;
	}
	GLuint rb, fbo;
	glGenRenderbuffers(1, &rb);
	glBindRenderbuffer(GL_RENDERBUFFER, rb);
	rb_storage(GL_RENDERBUFFER, img);
	glGenFramebuffers(1, &fbo);
	glBindFramebuffer(GL_FRAMEBUFFER, fbo);
	glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_RENDERBUFFER, rb);
	if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE) {
		printf("%s: FAIL (framebuffer incomplete)\n", name);
		return 0;
	}
	return 1;
}

static const float tri[] = { -0.5f, -0.5f, 0.5f, -0.5f, 0.0f, 0.5f };
static const float quad[] = { -1, -1, 1, -1, -1, 1, 1, 1 };

static void scenes(const char *prefix, GLuint solid, GLuint tex, GLuint texprog, unsigned int amask)
{
	static const int clear_pts[][2] = { { 0, 0 }, { 128, 128 }, { 255, 255 } };
	static const unsigned int clear_want[] = { 0x336699ff, 0x336699ff, 0x336699ff };
	static const int tri_pts[][2] = { { 128, 100 }, { 5, 250 }, { 250, 5 } };
	static const unsigned int tri_want[] = { 0xff0000ff, 0x336699ff, 0x336699ff };
	static const int tex_pts[][2] = { { 64, 64 }, { 192, 64 }, { 64, 192 }, { 192, 192 } };
	static const unsigned int tex_want[] = { 0xff0000ff, 0x00ff00ff, 0x0000ffff, 0xffffffff };
	char name[64];

	glViewport(0, 0, W, H);
	glClearColor(0.2f, 0.4f, 0.6f, 1.0f);
	glClear(GL_COLOR_BUFFER_BIT);
	snprintf(name, sizeof(name), "%sclear", prefix);
	check(name, clear_pts, clear_want, 3, amask);

	glUseProgram(solid);
	glUniform4f(glGetUniformLocation(solid, "col"), 1, 0, 0, 1);
	glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, tri);
	glEnableVertexAttribArray(0);
	glDrawArrays(GL_TRIANGLES, 0, 3);
	snprintf(name, sizeof(name), "%striangle", prefix);
	check(name, tri_pts, tri_want, 3, amask);

	if (!tex)
		return;

	/* 0.5 green over 0x336699: 0.5*0 + 0.5*0x33, 0.5*0xff + 0.5*0x66, ... */
	glClear(GL_COLOR_BUFFER_BIT);
	glEnable(GL_BLEND);
	glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
	glUniform4f(glGetUniformLocation(solid, "col"), 0, 1, 0, 0.5f);
	glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, quad);
	glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
	glDisable(GL_BLEND);
	{
		static const int pts[][2] = { { 128, 128 } };
		static const unsigned int want[] = { 0x19b24c00 };
		snprintf(name, sizeof(name), "%sblend", prefix);
		check(name, pts, want, 1, 0xff);
	}

	glUseProgram(texprog);
	glBindTexture(GL_TEXTURE_2D, tex);
	glUniform1i(glGetUniformLocation(texprog, "t"), 0);
	glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, quad);
	glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
	snprintf(name, sizeof(name), "%stexture", prefix);
	check(name, tex_pts, tex_want, 4, amask);
}

int main(void)
{
	int fd = open_node();
	if (fd < 0) {
		printf("no powervr render node\nFAIL\n");
		return 1;
	}
	struct gbm_device *gbm = gbm_create_device(fd);
	if (!gbm) {
		printf("gbm_create_device failed\nFAIL\n");
		return 1;
	}
	printf("gbm backend: %s\n", gbm_device_get_backend_name(gbm));

	PFNEGLGETPLATFORMDISPLAYEXTPROC get_display =
		(void *)eglGetProcAddress("eglGetPlatformDisplayEXT");
	EGLDisplay dpy = get_display(EGL_PLATFORM_GBM_KHR, gbm, NULL);
	EGLint major, minor;
	if (dpy == EGL_NO_DISPLAY || !eglInitialize(dpy, &major, &minor)) {
		printf("eglInitialize failed (0x%x)\nFAIL\n", eglGetError());
		return 1;
	}
	printf("EGL %d.%d %s\n", major, minor, eglQueryString(dpy, EGL_VENDOR));
	const char *ext = eglQueryString(dpy, EGL_EXTENSIONS);
	int has_dmabuf = strstr(ext, "EGL_EXT_image_dma_buf_import") != NULL;

	eglBindAPI(EGL_OPENGL_ES_API);
	static const EGLint ctx_attr[] = { EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE };
	EGLContext ctx = eglCreateContext(dpy, EGL_NO_CONFIG_KHR, EGL_NO_CONTEXT, ctx_attr);
	if (ctx == EGL_NO_CONTEXT || !eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, ctx)) {
		printf("context failed (0x%x)\nFAIL\n", eglGetError());
		return 1;
	}
	printf("GL_RENDERER: %s\nGL_VERSION: %s\n", glGetString(GL_RENDERER),
	       glGetString(GL_VERSION));

	GLuint solid = program("attribute vec2 pos; void main() { gl_Position = vec4(pos, 0.0, 1.0); }",
			       "precision mediump float; uniform vec4 col;"
			       "void main() { gl_FragColor = col; }");
	GLuint texprog = program("attribute vec2 pos; varying vec2 uv;"
				 "void main() { uv = pos * 0.5 + 0.5; gl_Position = vec4(pos, 0.0, 1.0); }",
				 "precision mediump float; uniform sampler2D t; varying vec2 uv;"
				 "void main() { gl_FragColor = texture2D(t, uv); }");

	/* bottom row red, green; top row blue, white (GL's first row is the bottom) */
	static const unsigned char texels[] = { 255, 0, 0, 255, 0, 255, 0, 255,
						0, 0, 255, 255, 255, 255, 255, 255 };
	GLuint tex, target, fbo;
	glGenTextures(1, &tex);
	glBindTexture(GL_TEXTURE_2D, tex);
	glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
	glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, 2, 2, 0, GL_RGBA, GL_UNSIGNED_BYTE, texels);
	glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
	glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
	glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
	glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);

	glGenTextures(1, &target);
	glBindTexture(GL_TEXTURE_2D, target);
	glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, W, H, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
	glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
	glGenFramebuffers(1, &fbo);
	glBindFramebuffer(GL_FRAMEBUFFER, fbo);
	glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, target, 0);
	if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE) {
		printf("texture framebuffer incomplete\nFAIL\n");
		return 1;
	}
	if (getenv("GLP_SCANOUT")) {
		unsigned int sw, sh;
		struct gbm_device *kgbm = NULL;
		struct gbm_bo *bo = NULL;
		uint64_t linear = DRM_FORMAT_MOD_LINEAR;
		int kfd = open_kms();

		if (sscanf(getenv("GLP_SCANOUT"), "%ux%u", &sw, &sh) != 2 || sw < W || sh < H) {
			printf("GLP_SCANOUT: WxH, at least %dx%d\nFAIL\n", W, H);
			return 1;
		}
		if (kfd >= 0)
			kgbm = gbm_create_device(kfd);
		if (kgbm) {
			printf("scanout gbm backend: %s\n", gbm_device_get_backend_name(kgbm));
			bo = gbm_bo_create_with_modifiers2(kgbm, sw, sh, GBM_FORMAT_XRGB8888, &linear, 1,
							   GBM_BO_USE_SCANOUT | GBM_BO_USE_RENDERING);
		}
		if (!kgbm || !bo) {
			printf("scanout: FAIL (%s)\nFAIL\n", kfd < 0 ? "no display device" :
			       !kgbm ? "gbm_create_device on the display" : "gbm_bo_create");
			return 1;
		}
		if (!has_dmabuf || !bind_buffer(dpy, bo, "scanout")) {
			printf("FAIL\n");
			return 1;
		}
		/* XRGB: alpha reads back as 1 or undefined, mask it */
		scenes("scanout-", solid, 0, 0, 0xff);
		/* the whole buffer: the last column and row, past a 64-byte boundary */
		const int corners[][2] = { { sw - 1, 0 }, { 0, sh - 1 }, { sw - 1, sh - 1 } };
		static const unsigned int want[] = { 0x336699ff, 0x336699ff, 0x336699ff };
		glViewport(0, 0, sw, sh);
		glClear(GL_COLOR_BUFFER_BIT);
		check("scanout-corners", corners, want, 3, 0xff);
		glFinish();
		printf("%s\n", failures ? "FAIL" : "PASS");
		return failures ? 1 : 0;
	}
	if (getenv("GLP_UPLOADS")) {
		const int rounds = atoi(getenv("GLP_UPLOADS"));
		const int size = getenv("GLP_UPLOAD_SIZE") ? atoi(getenv("GLP_UPLOAD_SIZE")) : 64;
		unsigned char *texels = size > 0 && size <= 4096 ? malloc((size_t)size * size * 4) : NULL;
		GLuint up;

		if (!texels) {
			printf("uploads: bad GLP_UPLOAD_SIZE\nFAIL\n");
			return 1;
		}

		glGenTextures(1, &up);
		glBindTexture(GL_TEXTURE_2D, up);
		glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, size, size, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
		glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
		glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
		glUseProgram(texprog);
		glUniform1i(glGetUniformLocation(texprog, "t"), 0);
		glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, quad);
		glEnableVertexAttribArray(0);
		glViewport(0, 0, W, H);
		for (int i = 0; i < rounds && !failures; i++) {
			/* a colour of its own per round, opaque */
			const unsigned char c[4] = { (unsigned char)(i * 37 + 11), (unsigned char)(i * 91 + 7),
						     (unsigned char)(i * 53 + 101), 255 };
			const unsigned char decoy[4] = { (unsigned char)~c[0], (unsigned char)~c[1],
							 (unsigned char)~c[2], 255 };

			for (int pass = 0; pass < 2; pass++) {
				for (int t = 0; t < size * size; t++)
					memcpy(texels + 4 * t, pass ? c : decoy, 4);
				glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, size, size, GL_RGBA,
						GL_UNSIGNED_BYTE, texels);
				glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
			}
			const unsigned int want = (unsigned int)c[0] << 24 | c[1] << 16 | c[2] << 8 | 255;
			const unsigned int got = pixel(W / 2, H / 2);
			if (!near(got, want)) {
				printf("uploads: FAIL in round %d got 0x%08x want 0x%08x\n", i, got, want);
				failures++;
			}
		}
		if (!failures)
			printf("uploads: %d rounds of %dx%d PASS\n", rounds, size, size);
		free(texels);
		printf("%s\n", failures ? "FAIL" : "PASS");
		return failures ? 1 : 0;
	}
	if (getenv("GLP_QUICK")) {
		/*
		 * GLP_QUICK=1: clear and triangle with no read in between (Zink
		 * folds the clear into the draw's render: load op CLEAR).
		 * GLP_QUICK=2: the clear read back first, so the triangle's render
		 * loads the target (load op LOAD), as in the full run.
		 */
		static const int pts[][2] = { { 128, 100 }, { 5, 250 } };
		static const unsigned int want[] = { 0xff0000ff, 0x336699ff };

		glViewport(0, 0, W, H);
		glClearColor(0.2f, 0.4f, 0.6f, 1.0f);
		glClear(GL_COLOR_BUFFER_BIT);
		if (atoi(getenv("GLP_QUICK")) == 2)
			printf("quick: clear read 0x%08x\n", pixel(0, 0));
		/*
		 * GLP_FS: the red from a uniform (default), a constant ("const"),
		 * or a varying the vertex shader sets ("varying")
		 */
		const char *fs = getenv("GLP_FS") ? getenv("GLP_FS") : "uniform";
		if (!strcmp(fs, "const"))
			glUseProgram(program("attribute vec2 pos;"
					     "void main() { gl_Position = vec4(pos, 0.0, 1.0); }",
					     "precision mediump float;"
					     "void main() { gl_FragColor = vec4(1.0, 0.0, 0.0, 1.0); }"));
		else if (!strcmp(fs, "varying"))
			glUseProgram(program("attribute vec2 pos; varying vec4 c;"
					     "void main() { c = vec4(1.0, 0.0, 0.0, 1.0);"
					     " gl_Position = vec4(pos, 0.0, 1.0); }",
					     "precision mediump float; varying vec4 c;"
					     "void main() { gl_FragColor = c; }"));
		else {
			glUseProgram(solid);
			glUniform4f(glGetUniformLocation(solid, "col"), 1, 0, 0, 1);
		}
		printf("quick: fragment colour from %s\n", fs);
		glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, tri);
		glEnableVertexAttribArray(0);
		glDrawArrays(GL_TRIANGLES, 0, 3);
		check("quick-triangle", pts, want, 2, 0);
		printf("%s\n", failures ? "FAIL" : "PASS");
		return failures ? 1 : 0;
	}
	scenes("", solid, tex, texprog, 0);

	/* the compositor's path: a GBM buffer imported as a render buffer */
	if (!has_dmabuf) {
		printf("dmabuf: FAIL (no EGL_EXT_image_dma_buf_import)\n");
		failures++;
	} else {
		uint64_t linear = DRM_FORMAT_MOD_LINEAR;
		struct gbm_bo *bo = gbm_bo_create_with_modifiers2(gbm, W, H, GBM_FORMAT_XRGB8888,
								  &linear, 1, GBM_BO_USE_RENDERING);
		if (!bo) {
			printf("dmabuf: FAIL (gbm_bo_create)\n");
			failures++;
		} else if (!bind_buffer(dpy, bo, "dmabuf")) {
			failures++;
		} else {
			/* XRGB: alpha reads back as 1 or undefined, mask it */
			scenes("dmabuf-", solid, 0, 0, 0xff);
		}
	}
	glFinish();
	printf("%s\n", failures ? "FAIL" : "PASS");
	return failures ? 1 : 0;
}
