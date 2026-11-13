#version 450
// the colour from 16 bytes of push constants (vk-tri TRI_PUSH=R,G,B,A)
layout(push_constant) uniform P { vec4 c; } p;
layout(location = 0) out vec4 o;
void main()
{
	o = p.c;
}
