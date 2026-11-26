"""Ground speed from the video (in camera heights per second) against the motor-rpm wheel speed. The scale (camera
height) is set on coasting stretches, where slip is ~0; after that, the ratio wheel/ground is the drive or brake slip."""
import numpy as np, pandas as pd, json
from common import video_t0
SID = '20260813_001309_427966'; LAG = 1.67
tw0 = json.load(open('out/tw0_0813.json'))['tw0']
G = pd.concat([pd.read_csv(f'out/ground_0813_{k}.csv') for k in 'abc']).drop_duplicates('frame').sort_values('frame')
G['t'] = video_t0(SID) + G.frame / 15 + LAG - tw0
G['vg_h'] = pd.Series((G.ds_h * 15).values).rolling(5, center=True, min_periods=3).median().values
o = pd.read_csv('clean/20260813_001309.csv.gz'); sm = lambda x, n: pd.Series(x).rolling(n, center=True, min_periods=1).mean().values
v = sm(o.vehicle_speed_kmh.clip(lower=0).values / 3.6, 15); tq = sm(o.INV_torque_feedback.values, 15)
ax = sm(o.x_g.values, 30); ax *= np.sign(np.corrcoef(ax[v > 3], np.gradient(sm(v, 100))[v > 3])[0, 1])
G['v'] = np.interp(G.t, o.t, v); G['tq'] = np.interp(G.t, o.t, tq); G['ax'] = np.interp(G.t, o.t, ax)
ok = (G.n >= 20) & (G.v > 5) & (G.ds_h_iqr < 0.5 * G.ds_h.abs() + 0.05)
coast = ok & (G.tq.abs() < 5) & (G.ax.abs() < 0.08)
h = np.median(G.v[coast] / G.vg_h[coast]); print('frames', len(G), 'usable', int(ok.sum()), 'coasting', int(coast.sum()), '-> camera height from coasting %.2f m' % h)
for lo, hi in ((5, 10), (10, 15), (15, 20), (20, 26)):
    m = coast & (G.v >= lo) & (G.v < hi)
    allv = (G.v >= lo) & (G.v < hi); print('  %d-%d m/s: usable frames %.0f%%' % (lo, hi, 100 * (ok & allv).sum() / max(allv.sum(), 1)))
    if m.sum() < 5: continue
    print('  coasting %d-%d m/s: wheel/ground ratio %.3f (n %d, IQR %.3f)' % (lo, hi, np.median(G.v[m] / (h * G.vg_h[m])), m.sum(), np.subtract(*np.percentile(G.v[m] / (h * G.vg_h[m]), [75, 25]))))
G['slip'] = G.v / (h * G.vg_h) - 1
rows = []
for name, m in (('coasting', coast), ('drive 50-100 Nm', ok & (G.tq >= 50) & (G.tq < 100)), ('drive 100-150 Nm', ok & (G.tq >= 100) & (G.tq < 150)), ('drive >150 Nm', ok & (G.tq >= 150)), ('regen / braking (torque < -5 Nm or ax < -0.3 g)', ok & ((G.tq < -5) | (G.ax < -0.3)))):
    s = G.slip[m]; rows.append(dict(state=name, frames=int(m.sum()), slip_median_pct=round(100 * s.median(), 1), slip_p25_pct=round(100 * s.quantile(.25), 1), slip_p75_pct=round(100 * s.quantile(.75), 1)))
T = pd.DataFrame(rows); print(T.to_string(index=False)); T.to_csv('out/ground_slip.csv', index=False)
G.to_csv('out/ground_0813.csv', index=False); json.dump(dict(h_coast=float(h)), open('out/ground_scale.json', 'w'))
