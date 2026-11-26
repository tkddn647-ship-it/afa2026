"""Usage: python3 uart_sim.py "<repo>/device/raspberry pi 5" <sensor_log_20260731_034542_*.csv.gz> out/uart_sim.csv
Bit-error simulation of the STM32 -> Pi UART link with the Pi's real (old) line parser.
Lines are rebuilt from logged rows in the firmware's own number formats, the byte stream gets independent bit
flips, then it is split and parsed exactly like Uart_stm.read_loop/parse_line."""
import sys, types, numpy as np, pandas as pd
sys.path.insert(0, sys.argv[1])
fake = types.ModuleType("serial"); fake.Serial = object; fake.SerialException = Exception
tools = types.ModuleType("serial.tools"); lp = types.ModuleType("serial.tools.list_ports"); lp.comports = lambda: []
tools.list_ports = lp; fake.tools = tools; sys.modules.update({"serial": fake, "serial.tools": tools, "serial.tools.list_ports": lp})
import Uart_stm as U
FMT = [('FR',2),('FL',2),('RR',2),('RL',2),('x_g',3),('y_g',3),('z_g',3),('ecu_temp',1),('steering_angle',1),('steering_speed',0),
       ('inv_temp_igt',1),('inv_temp_motor',1),('inv_motor_speed',0),('inv_dc_current',1),('inv_voltage',1),('motor_precharge',0),('motor_main_contactor',0),('motor_inverter_mode',0),
       ('INV_shudder_torque',1),('INV_id_feedback',1),('INV_iq_feedback',1),('INV_torque_commanded',1),('INV_torque_feedback',1),('INV_id_command',1),('INV_iq_command',1),
       ('bms_charge',1),('bms_voltage',1),('bms_current',1),('bms_ccl',0),('bms_dcl',0),('bms_temp_maxvalue',1),('bms_capacity',1),('can1',0),('can2',0)]
LIM = dict(FR=60,FL=60,RR=60,RL=60,x_g=4,y_g=4,z_g=4,ecu_temp=95,inv_temp_igt=120,inv_temp_motor=150,inv_motor_speed=7000,inv_dc_current=450,INV_iq_command=600,INV_iq_feedback=600,INV_id_feedback=300,INV_torque_commanded=260,INV_torque_feedback=260)
def fmt(v, dec):
    v = round(float(v), dec)
    return f'{int(v)}' if dec == 0 else f'{v:.{dec}f}'
def build_lines(path, n=3000):
    """path: a sensor_log (rows 20000-23000 are used) or a file already cut to those rows"""
    d = pd.read_csv(path, low_memory=False)
    if len(d) > 5000: d = d.iloc[20000:23000]
    d = d.dropna(subset=['stm_ms'])
    good = np.ones(len(d), bool)
    for k, lim in LIM.items(): good &= np.abs(pd.to_numeric(d[k], errors='coerce').fillna(0).values) <= lim
    d = d[good].head(n).copy(); d['INV_shudder_torque'] = 0.0   # always 0 on this car; non-zero values in the log are bit flips
    lines = []; truth = []
    for _, r in d.iterrows():
        vals = [fmt(r[k], dec) for k, dec in FMT]; line = f"{int(r.stm_ms)}," + ",".join(vals)
        lines.append(line); truth.append([int(r.stm_ms)] + [float(x) for x in vals])
    return lines, np.array(truth, float)
def add_crc(lines):
    return [f"{l},*{U.line_crc16(l.encode()):04X}" for l in lines]
def _unused():
    return lines, np.array(truth, float)
def run(lines, truth, p, rng, guard=False, reps=1):
    out = dict(sent=0, correct=0, wrong=0, lost=0, visible=0, flips=0)
    keys = [k for k, _ in FMT]; by_stm = {int(t[0]): t for t in truth}
    for _ in range(reps):
        raw = bytearray(("\n".join(lines) + "\n").encode())
        bits = np.unpackbits(np.frombuffer(bytes(raw), np.uint8)); flip = rng.random(bits.size) < p
        raw = np.packbits(bits ^ flip).tobytes()
        got = {}; G = U.LineGuard()
        for chunk in raw.split(b"\n"):
            line = chunk.decode("ascii", errors="ignore")
            if U.is_boot_line(line): continue
            if guard:
                line, _ = G.check(line)
                if line is None: continue
            s = U.parse_line(line)
            if s is None or 'stm_ms' not in s: 
                if s is not None: out['wrong'] += 1
                continue
            vec = np.array([s['stm_ms']] + [s.get(k if k[:4] != 'INV_' else 'inv_' + k[4:], s.get(k, 0.0)) for k in keys], float)
            t = by_stm.get(int(s['stm_ms']))
            if t is not None and np.allclose(vec, t, atol=1e-6) and int(s['stm_ms']) not in got: got[int(s['stm_ms'])] = 1; out['correct'] += 1
            else:
                out['wrong'] += 1
                if any(abs(vec[1 + keys.index(k)]) > lim for k, lim in LIM.items()): out['visible'] += 1
                if t is not None: got.setdefault(int(s['stm_ms']), 0)
            sh = vec[1 + keys.index('INV_shudder_torque')]
            if round(sh, 3) in (0.1, 0.2, 0.4, 0.8, 1, 2, 4, 8): out['flips'] += 1
        out['sent'] += len(lines); out['lost'] += len(lines) - sum(1 for v in got.values() if v == 1) - sum(1 for v in got.values() if v == 0)
    acc = out['correct'] + out['wrong']
    return dict(p=p, loss_pct=100 * (out['sent'] - acc) / out['sent'], wrong_of_kept_pct=100 * out['wrong'] / max(acc, 1),
                visible_per_1000=1000 * out['visible'] / max(acc, 1), p_bit_measured=out['flips'] / max(acc, 1) / 8, mean_line_bytes=np.mean([len(l) + 1 for l in lines]))
if __name__ == '__main__':
    lines, truth = build_lines(sys.argv[2]); rng = np.random.default_rng(1)
    print('lines', len(lines), 'mean bytes', np.mean([len(l) + 1 for l in lines]))
    P_ = (0, 5e-5, 1e-4, 1.5e-4, 2e-4, 3e-4, 4e-4)
    R = pd.concat([pd.DataFrame([run(lines, truth, p, rng, reps=3) for p in P_]).assign(parser='old (no checksum)'),
                   pd.DataFrame([run(add_crc(lines), truth, p, rng, guard=True, reps=3) for p in P_]).assign(parser='new (CRC + field check)')])
    print(R.round(5).to_string(index=False)); R.to_csv(sys.argv[3] if len(sys.argv) > 3 else 'uart_sim.csv', index=False)
