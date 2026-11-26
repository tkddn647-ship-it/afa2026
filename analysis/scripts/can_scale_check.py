"""Is the inverter DC current measured or calculated? Cascadia CAN Protocol 5.9: 0x0A6 B6-7 'The calculated DC Bus current',
0x0AC B2-3 torque feedback 'estimated ... based on motor parameters'. Check against the Orion hall-sensor current."""
import numpy as np, pandas as pd, glob, os, re
from common import load_sensor
sids=sorted({re.search(r'sensor_log_(\d{8}_\d{6}_\d+)',f).group(1) for f in glob.glob('../udp_logger/logs/sensor_log_2026*.csv*') if re.search(r'sensor_log_(\d{8}_\d{6}_\d+)',f)})
rows=[]; allp=[]
for sid in sids:
    try: d,_=load_sensor(sid)
    except Exception as e: continue
    q=lambda c: pd.to_numeric(d.get(c),errors='coerce')
    T=q('INV_torque_feedback'); rpm=q('inv_motor_speed'); Ii=q('inv_dc_current'); Vi=q('inv_voltage'); Ib=q('bms_current'); Vb=q('bms_voltage'); iq=q('INV_iq_feedback')
    w=rpm.abs()*2*np.pi/60; Pm=T*w*np.sign(rpm.replace(0,1)); Pi=Ii*Vi; Pb=Ib*Vb
    ok=(Pm>5000)&(Ii.between(20,400))&(Ib.between(10,400))&(Vi>100)&(Vb>100)
    if ok.sum()<300: continue
    ei=(Pm/Pi)[ok]; eb=(Pm/Pb)[ok]; k=(Ii/Ib)[ok]
    p=pd.DataFrame(dict(sid=sid[:15],rpm=rpm[ok].abs(),T=T[ok],ei=ei,eb=eb,k=k)); allp.append(p)
    # coasting / zero torque with contactor closed, rolling
    co=(T.abs()<2)&(rpm.abs()>500)&(Vb>100)
    st=(rpm.abs()<5)&(T.abs()<1)&(Vb>100)
    rel=lambda s:(s.quantile(.75)-s.quantile(.25))/s.median()
    rows.append(dict(session=sid[:15],n=int(ok.sum()),eff_inv_med=ei.median(),eff_inv_iqr_rel=rel(ei),eff_bms_med=eb.median(),eff_bms_iqr_rel=rel(eb),
        inv_over_bms=k.median(),coast_n=int(co.sum()),coast_inv_A=Ii[co].median(),coast_bms_A=Ib[co].median(),stand_n=int(st.sum()),stand_inv_A=Ii[st].median(),stand_bms_A=Ib[st].median(),
        T_over_iq=(T/iq)[ok&(iq>20)].median(), T_over_iq_iqr_rel=rel((T/iq)[ok&(iq>20)])))
R=pd.DataFrame(rows); pd.set_option('display.width',250); print(R.round(3).to_string(index=False))
P=pd.concat(allp)
P['rpm_bin']=pd.cut(P.rpm,[0,2000,3000,4000,5000,6000,8000]); P['T_bin']=pd.cut(P['T'],[0,40,80,120,160,250])
print(P.groupby('rpm_bin',observed=True)[['ei','eb','k']].median().round(3))
print(P.groupby('T_bin',observed=True)[['ei','eb','k']].median().round(3))
os.makedirs('out',exist_ok=True); R.to_csv('out/can_scale_check.csv',index=False)
