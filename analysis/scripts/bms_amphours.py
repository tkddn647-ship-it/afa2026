"""Pack Amphours from the Orion BMS (CAN 0x081 byte 1, logged as bms_capacity) against the integrated currents.
The firmware decodes it with x1; the values (121-176 for a ~270 V Formula Student pack) only make sense as 0.1 Ah units,
so they are divided by 10 here (inference, checked against the integrated currents below)."""
import numpy as np, pandas as pd
from common import load_sensor
rows = []
for sid in ('20260811_025402_438882','20260811_071828_103551','20260811_073314_333336','20260811_082807_765988','20260811_084517_257245','20260813_001309_427966'):
    try: d, _ = load_sensor(sid)
    except Exception as e: print(sid, 'skip', e); continue
    d = d.sort_values('t_wall'); t = d.t_wall.values; q = lambda c: pd.to_numeric(d[c], errors='coerce').values
    cap = q('bms_capacity'); cap = np.where((cap > 50) & (cap < 255), cap, np.nan) / 10
    capm = pd.Series(cap).rolling(301, center=True, min_periods=50).median().values
    def integ(i):
        i = np.where(np.abs(i) < 500, i, np.nan); i = pd.Series(i).interpolate(limit=50).fillna(0).values
        return float(np.sum(i[1:] * np.minimum(np.diff(t), 0.5)) / 3600)
    n = int(60 / 0.012); a, b = np.nanmedian(capm[:n]), np.nanmedian(capm[-n:])
    ib, ii = integ(q('bms_current')), integ(q('inv_dc_current'))
    rows.append(dict(session=sid[:15], minutes=round((t[-1] - t[0]) / 60, 1), amphours_start=round(a, 1), amphours_end=round(b, 1), drop_Ah=round(a - b, 1), int_bms_Ah=round(ib, 2), int_inverter_Ah=round(ii, 2),
                     bms_over_drop=round(ib / (a - b), 2) if a - b > 0.25 else np.nan, inverter_over_drop=round(ii / (a - b), 2) if a - b > 0.25 else np.nan))
T = pd.DataFrame(rows); T.to_csv('out/bms_amphours.csv', index=False); print(T.to_string(index=False))
