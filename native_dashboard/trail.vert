void MAIN()
{
    vec4 a = VIEW_MATRIX * MODEL_MATRIX * vec4(VERTEX - NORMAL * UV0.x, 1.0);
    vec4 b = VIEW_MATRIX * MODEL_MATRIX * vec4(VERTEX + NORMAL * (1.0 - UV0.x), 1.0);
    // Clip before perspective division so a segment crossing the eye cannot explode.
    float nearZ = -CAMERA_PROPERTIES.x - 0.001;
    if (a.z > nearZ && b.z > nearZ) {
        POSITION = vec4(2.0, 2.0, 2.0, 1.0);
        return;
    }
    if (a.z > nearZ)
        a = mix(a, b, (nearZ - a.z) / (b.z - a.z));
    if (b.z > nearZ)
        b = mix(b, a, (nearZ - b.z) / (a.z - b.z));
    a = PROJECTION_MATRIX * a;
    b = PROJECTION_MATRIX * b;
    vec2 viewport = max(viewportSize, vec2(1.0));
    vec2 delta = (b.xy / b.w - a.xy / a.w) * viewport;
    float lengthPx = length(delta);
    vec2 tangent = lengthPx > 0.0001 ? delta / lengthPx : vec2(1.0, 0.0);
    vec2 normal = vec2(-tangent.y, tangent.x);
    POSITION = mix(a, b, UV0.x);
    POSITION.xy += (normal * UV0.y + tangent * (2.0 * UV0.x - 1.0))
                   * lineWidth / viewport * POSITION.w;
}
