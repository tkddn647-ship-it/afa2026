"""Apply the spike scrubber to every session and show what it changes. Channels whose single-sample spikes cannot be
physical are scrubbed: potentiometers (the firmware EMA cannot jump one sample and come back), temperatures and voltages
(slow), CAN drive values (sample-and-hold of CAN frames). The accelerometer is left as is: an isolated spike there
can be a real bump. Writes clean/<session>_scrub_mask.csv.gz (row index + channel) and out/scrub_effect.csv."""
import numpy as np, pandas as pd, glob
from scrub import DEC, spikes
SCRUB = [c for c in DEC if c not in ('x_g', 'y_g', 'z_g')]
# second pass: runs of corrupted rows that the spike test misses, removed by physical limits
LIMIT = dict(FR=100, FL=100, RR=100, RL=100, ecu_temp=90, inv_temp_igt=120, inv_temp_motor=150, inv_motor_speed=7000, inv_dc_current=450,
             INV_iq_feedback=600, INV_torque_feedback=260, INV_torque_commanded=260, bms_current=450, inv_voltage=400, bms_voltage=400)
def peaks(d, cols):
    q = lambda c: np.abs(pd.to_numeric(d[c], errors='coerce').values.astype(float))
    out = {}
    for c in cols:
        if c in d: v = q(c); v = v[np.isfinite(v)]; out[c + '_max'] = float(v.max()) if len(v) else np.nan; out[c + '_p999'] = float(np.quantile(v, .999)) if len(v) else np.nan
    return out
rows = []
for f in sorted(glob.glob('../udp_logger/logs/sensor_log_2026*.csv.gz')):
    sid = f.split('sensor_log_')[1][:15]
    if sid < '20260728': continue
    d = pd.read_csv(f, low_memory=False); s = pd.to_numeric(d.stm_ms, errors='coerce').values; d = d.iloc[np.argsort(s, kind='stable')].reset_index(drop=True)
    if len(d) < 3000: continue
    cols = ['FR', 'RR', 'inv_motor_speed', 'inv_dc_current', 'inv_temp_motor', 'ecu_temp', 'bms_current']
    before = peaks(d, cols); e = d.copy(); mask = []
    for ch in SCRUB:
        if ch not in d: continue
        x = pd.to_numeric(d[ch], errors='coerce').values.astype(float)
        if np.nanstd(x) == 0: continue
        m, _ = spikes(x, ch); lim = (np.abs(x) > LIMIT[ch]) & ~m if ch in LIMIT else np.zeros(len(x), bool)
        e.loc[m | lim, ch] = np.nan; mask += [(i, ch, 'spike') for i in np.flatnonzero(m)] + [(i, ch, 'limit') for i in np.flatnonzero(lim)]
    after = peaks(e, cols)
    pd.DataFrame(mask, columns=['row', 'channel', 'reason']).to_csv(f'clean/{sid}_scrub_mask.csv.gz', index=False)
    r = dict(session=sid, rows=len(d), spikes=sum(1 for m_ in mask if m_[2] == 'spike'), over_limit=sum(1 for m_ in mask if m_[2] == 'limit'), per_1000_rows=round(1000 * len(mask) / len(d), 2))
    for k in before:
        if k.endswith('_max'): r[k.replace('_max', '') + ' max before→after'] = f"{before[k]:.0f}→{after[k]:.0f}" if np.isfinite(before[k]) else ''
    rows.append(r); print(sid, r['per_1000_rows'], {k: v for k, v in r.items() if '→' in str(v)})
pd.DataFrame(rows).to_csv('out/scrub_effect.csv', index=False)
