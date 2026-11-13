#version 450
// vk-tri's inputs, no varyings, but col read as well (its alpha, 1.0 in
// vk-tri's vertices, as w): the vertex fetches of vk-tri's own shader
layout(location = 0) in vec2 pos;
layout(location = 1) in vec4 col;
void main()
{
	gl_Position = vec4(pos, 0.0, col.a);
}
