#version 450
// vk-tri's inputs, position only: no varyings (as Zink's gl-probe draw)
layout(location = 0) in vec2 pos;
layout(location = 1) in vec4 col;
void main()
{
	gl_Position = vec4(pos, 0.0, 1.0);
}
