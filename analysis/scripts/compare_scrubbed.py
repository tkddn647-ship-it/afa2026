"""Does the bit-flip scrubber change any conclusion? The whole pipeline (clean -> laps -> corners -> roll -> understeer ->
powertrain) was rerun in ../_analysis_s, where common.load_sensor applies scrub.py/scrub_apply.py first. This compares the outputs."""
import pandas as pd, numpy as np, json
A, B = 'out', '../_analysis_s/out'; rows = []
ca, cb = json.load(open(f'{A}/courses.json')), json.load(open(f'{B}/courses.json'))
for k in ca:
    for m in ('best', 'theoretical', 'n'): rows.append(dict(result=f'course {k[0]} {m}', before=round(ca[k][m], 2), after=round(cb[k][m], 2)))
la, lb = pd.read_csv(f'{A}/laps_courses.csv'), pd.read_csv(f'{B}/laps_courses.csv')
for c in ('A', 'B'): rows.append(dict(result=f'course {c} median lap s', before=round(la[la.course == c].time_s.median(), 2), after=round(lb[lb.course == c].time_s.median(), 2)))
ya, yb = json.load(open(f'{A}/yaw_model.json')), json.load(open(f'{B}/yaw_model.json'))
for k in ('camera', 'ayv'): rows.append(dict(result=f'understeer K ({k})', before=ya[k]['K'], after=yb[k]['K']))
ra, rb = pd.read_csv(f'{A}/roll_gradient.csv'), pd.read_csv(f'{B}/roll_gradient.csv')
rows.append(dict(result='front roll mm/g, max change over sessions', before='', after=f"{np.abs(rb.front_mm_per_g - ra.front_mm_per_g).max():.2f} mm/g"))
ka, kb = pd.read_csv(f'{A}/corners_courses.csv'), pd.read_csv(f'{B}/corners_courses.csv')
for c in ('v_min', 'ay_peak_g', 'radius_m', 'seg_time_best'):
    r = (kb[c] - ka[c]).abs() / ka[c].abs(); i = int(r.idxmax())
    rows.append(dict(result=f'corner {c}, largest change', before=f"{ka.course[i]}{ka.corner[i]} {ka[c][i]}", after=f"{kb[c][i]} ({100 * r[i]:.1f}%), median change {100 * r.median():.1f}%"))
for f, cols in (('efficiency.csv', ('eff_vs_inverter_current', 'Iinv_over_Ibms')), ('powertrain.csv', ('E_Wh', 'P_p999_kW')), ('mass.csv', ('m_eff_kg_from_rpm',))):
    x, y = pd.read_csv(f'{A}/{f}').drop_duplicates('session'), pd.read_csv(f'{B}/{f}').drop_duplicates('session'); m = x.merge(y, on='session', suffixes=('', '_s'))
    for c in cols: rows.append(dict(result=f'{c}, max change over sessions', before='', after=f"{100 * ((m[c + '_s'] - m[c]).abs() / m[c].abs()).max():.1f}%"))
T = pd.DataFrame(rows); T.to_csv('out/scrub_robustness.csv', index=False); print(T.to_string(index=False))
