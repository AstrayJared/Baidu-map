import math


def radial(origin, angle, radius):
    return origin[0] + radius * math.cos(angle), origin[1] + radius * math.sin(angle)


def halton(index, base):
    value, fraction = 0., 1.
    while index:
        fraction /= base
        value += fraction * (index % base)
        index //= base
    return value


def exploratory_points(origin, radius, count):
    """Square coverage including four corner witnesses, then a Halton design."""
    from .extent import EDGE_INSET_M
    half = max(0, radius - EDGE_INSET_M)
    corners = ((-1, -1), (1, -1), (1, 1), (-1, 1))
    for x, y in corners[:count]:
        yield origin[0] + x * half, origin[1] + y * half
    for i in range(1, max(0, count - 4) + 1):
        yield origin[0] + half * (2 * halton(i, 2) - 1), origin[1] + half * (2 * halton(i, 3) - 1)


def normal_pairs(geometry, distance, count, *, include_holes=True):
    if geometry.is_empty or count <= 0:
        return
    rings = [r for polygon in geometry.geoms for r in
             ([polygon.exterior, *polygon.interiors] if include_holes else [polygon.exterior])]
    total = sum(r.length for r in rings)
    # Largest-remainder allocation enforces the requested total even with many
    # holes/components; previously every ring received at least one pair.
    shares = [count * ring.length / total for ring in rings]
    allocated = [int(value) for value in shares]
    for index in sorted(range(len(rings)), key=lambda i: (-(shares[i] - allocated[i]), i))[:count - sum(allocated)]:
        allocated[index] += 1
    for ring, n in zip(rings, allocated):
        for i in range(n):
            d = (i + .5) * ring.length / n
            p = ring.interpolate(d)
            a, b = ring.interpolate((d - 1) % ring.length), ring.interpolate((d + 1) % ring.length)
            dx, dy = b.x - a.x, b.y - a.y
            norm = math.hypot(dx, dy)
            if norm:
                yield (p.x - dy / norm * distance, p.y + dx / norm * distance), (p.x + dy / norm * distance, p.y - dx / norm * distance)
