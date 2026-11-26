"""UART line integrity per session from the logged rows alone.
lost      : samples missing from the STM 10 ms sequence (periodic 5 % server-filter loss removed)
garbled   : rows that parsed but carry physically impossible values (the Pi parser has no checksum)
flip      : in fields that are 0 for the whole session, which digit replaced '0' (single-bit XOR pattern)"""
import pandas as pd, numpy as np, glob, sys, json
K=['FR','FL','RR','RL','x_g','y_g','z_g','ecu_temp','steering_angle','steering_speed','inv_temp_igt','inv_temp_motor','inv_motor_speed','inv_dc_current','inv_voltage','INV_shudder_torque','INV_id_feedback','INV_iq_feedback','INV_torque_commanded','INV_torque_feedback','INV_id_command','INV_iq_command','bms_charge','bms_voltage','bms_current','bms_ccl','bms_dcl','bms_temp_maxvalue','bms_capacity']
LIM=dict(FR=60,FL=60,RR=60,RL=60,x_g=4,y_g=4,z_g=4,ecu_temp=95,inv_temp_igt=120,inv_temp_motor=150,inv_motor_speed=7000,inv_dc_current=450,INV_iq_command=600,INV_iq_feedback=600,INV_id_feedback=300,INV_torque_commanded=260,INV_torque_feedback=260)
def run(f):
    d=pd.read_csv(f,low_memory=False); sid=f.split('sensor_log_')[1][:15]
    s=d.stm_ms.values.astype(float); n=len(d)
    nostm=np.isnan(s); med=pd.Series(s).rolling(41,center=True,min_periods=5).median().values; badstm=~nostm&(np.abs(s-med)>3000)
    KK=[k for k in K if k in d]; X=d[KK].apply(pd.to_numeric,errors='coerce').values
    phys=np.zeros(n,bool)
    for k,lim in LIM.items():
        if k in KK: j=KK.index(k); phys|=np.abs(X[:,j])>lim
    # sudden excursions on slow channels (temperatures, voltage): > 25 % from local median
    for k in ('ecu_temp','inv_temp_igt','inv_temp_motor','inv_voltage'):
        if k not in KK: continue
        j=KK.index(k); m=pd.Series(X[:,j]).rolling(21,center=True,min_periods=5).median().values; phys|=(m>10)&(np.abs(X[:,j]-m)>0.25*m)
    zero_row=np.nansum(np.abs(X),1)==0
    garbled=phys|badstm|nostm|zero_row
    # digit flips in fields that are exactly 0 in >= 99.5 % of rows
    flips={}
    for j,k in enumerate(KK):
        x=X[:,j]; z=(x==0).mean()
        if z>=0.995:
            v=x[(x!=0)&np.isfinite(x)]
            for val in v:
                if float(val).is_integer() and 1<=val<=9: flips[int(val)]=flips.get(int(val),0)+1
                else: flips['other']=flips.get('other',0)+1
    # loss from the STM sequence (clean rows only)
    ss=np.sort(s[~garbled]); ds=np.diff(ss); ds=ds[(ds>0)&(ds<5000)]; k=np.round(ds/10).astype(int)
    lost=int((k[k>=2]-1).sum()); periodic=int(((ds>=18)&(ds<=21)).sum()); tot=len(ss)+lost
    cur=pd.to_numeric(d.inv_dc_current,errors='coerce').values if 'inv_dc_current' in d else np.zeros(n); drive=np.nanmean((np.abs(cur)>20)&(np.abs(cur)<450))
    return dict(session=sid,rows=n,lost_pct=round(100*lost/tot,1),burst_pct=round(100*(lost-periodic)/tot,1),garbled_per_1000=round(1000*garbled.mean(),2),
                zero_rows=int(zero_row.sum()),no_stm=int(nostm.sum()),flips=flips,drive_share=round(float(drive),2))
if __name__=='__main__':
    rows=[]
    for f in sorted(glob.glob(sys.argv[1]+'/sensor_log_2026*.csv*')):
        try: r=run(f)
        except Exception as e: print(f[-40:],'ERR',e); continue
        if r['rows']<3000: continue
        rows.append(r); print(r)
    pd.DataFrame(rows).to_csv('out/uart_integrity.csv',index=False)
