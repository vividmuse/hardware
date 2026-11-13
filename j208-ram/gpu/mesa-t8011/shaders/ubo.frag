#version 450
// element 0 of vk-tri's TRI_UBO buffer: (0, 0.5, 1, 1)
layout(set = 0, binding = 0) uniform U { vec4 c[32]; } u;
layout(location = 0) out vec4 o;
void main()
{
	o = u.c[0];
}
