"""Bit error rate per session from INV_shudder_torque, a field that is 0 on this car: a logged 0.1/0.2/0.4/0.8/1/2/4/8
is "0.0" with one of the low four bits of one digit flipped. p = flips / (rows * 2 digits * 4 bits). Rows lost to an
error never reach the log, so this is the rate seen in surviving lines. Writes out/uart_pbit.csv."""
import pandas as pd, numpy as np, glob, sys
LOGS = sys.argv[1] if len(sys.argv) > 1 else '../udp_logger/logs'
R = pd.read_csv('out/uart_integrity.csv', dtype={'session': str}); out = []
for sid in R.session:
    f = [x for x in glob.glob(f'{LOGS}/sensor_log_{sid}*.csv*') if not x.endswith(('bak_steer','bak_steer_est'))][0]
    d = pd.read_csv(f, low_memory=False, usecols=lambda c: c in ('INV_shudder_torque', 'stm_ms'))
    if 'INV_shudder_torque' not in d: continue
    x = pd.to_numeric(d.INV_shudder_torque, errors='coerce').values
    if (x == 0).mean() < 0.995: continue
    single = np.isin(np.round(x, 3), [0.1, 0.2, 0.4, 0.8, 1, 2, 4, 8]).sum()
    out.append(dict(session=sid, rows=len(x), single_bit_flips=int(single), p_bit=single / (len(x) * 2 * 4)))
P = pd.DataFrame(out).merge(R[['session','lost_pct','burst_pct','garbled_per_1000','drive_share']], on='session'); P.to_csv('out/uart_pbit.csv', index=False)
print(P.to_string(index=False)); print('Spearman p_bit vs burst loss %.2f, vs garbled rows %.2f'%(P[['p_bit','burst_pct']].corr('spearman').iloc[0,1], P[['p_bit','garbled_per_1000']].corr('spearman').iloc[0,1]))
