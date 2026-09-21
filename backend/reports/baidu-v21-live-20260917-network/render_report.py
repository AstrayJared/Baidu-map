"""Render the saved audit, without making network calls."""
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from shapely.geometry import shape
from shapely.ops import transform
from life_circle.coordinates import LocalProjection

root = Path('reports/baidu-v21-live-20260917-network')
core = json.loads((root / 'result.json').read_text(encoding='utf-8'))
rows = json.loads((root / 'validation-results.json').read_text(encoding='utf-8'))
summary = json.loads((root / 'summary.json').read_text(encoding='utf-8'))
project = LocalProjection(core['config']['origin'])
g = transform(lambda x,y: project.to_local((x,y)), shape(core['geometry']))
fig, ax = plt.subplots(figsize=(10,9))
for polygon in g.geoms:
    x,y = polygon.exterior.xy
    ax.fill(x,y, color='#d7e9f6', alpha=.8)
    ax.plot(x,y, color='#286a96', linewidth=1)
    for ring in polygon.interiors:
        x,y = ring.xy
        ax.fill(x,y, color='white')
        ax.plot(x,y, color='#286a96', linewidth=1)
for label, color, marker, predicate in [
    ('Agreement (15s tolerance)', '#16845b', 'o', lambda r:r['valid'] and r['tolerant_correct']),
    ('False inclusion', '#db423b', '^', lambda r:r['valid'] and not r['tolerant_correct'] and r['inside']),
    ('False exclusion', '#9751b5', 's', lambda r:r['valid'] and not r['tolerant_correct'] and not r['inside']),
    ('Invalid reference', '#888888', 'x', lambda r:not r['valid']),
]:
    points=[project.to_local(r['coordinate']) for r in rows if predicate(r)]
    if points:
        ax.scatter(*zip(*points), c=color, marker=marker, s=32, label=f'{label} ({len(points)})', zorder=3)
ax.scatter([0],[0], marker='*', s=190, color='#111827', label='Origin', zorder=4)
ax.set_aspect('equal')
ax.set_xlabel('Approximate east offset (m)')
ax.set_ylabel('Approximate north offset (m)')
ax.set_title('Baidu road v2.1 | 15-minute walking area\n97 held-out reference points | 2026-09-17')
ax.legend(loc='best', fontsize=9)
ax.grid(alpha=.15)
fig.text(.5,.015,'Local BD09 sampling coordinates. No basemap. Invalid references excluded from accuracy.',ha='center',fontsize=9)
fig.tight_layout(rect=(0,.035,1,1))
fig.savefig(root/'validation-map.png', dpi=170)
