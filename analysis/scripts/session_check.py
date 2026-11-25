"""One-page health check for a sensor log.  Usage:
     python3 session_check.py <session_id> [...]      (e.g. 20260813_001309_427966)
     python3 session_check.py --all                   (every log from 7/28 on; writes out/session_check.md)
Checks that need no reference instrument: sample loss split, corrupt rows, clock scale, dead/noisy pot channels,
left/right label consistency, accelerometer and steering sign, current-sensor agreement."""
import numpy as np, pandas as pd, sys, glob, os, json
from common import *
sm=lambda x,n: pd.Series(x).rolling(n,center=True,min_periods=max(1,n//5)).mean().values
def check(sid):
    p=f'{ROOT}/udp_logger/logs/sensor_log_{sid}.csv.gz'; raw=pd.read_csv(p,low_memory=False); n0=len(raw); R=dict(session=sid[:15]); F=[]
    if n0<3000: return None
    s=raw.stm_ms.astype(float).values; medv=pd.Series(s).rolling(41,center=True,min_periods=5).median().values; okr=np.abs(s-medv)<3000; s2=s[okr]; ds=np.diff(s2); k=np.rint(ds/10); v=(ds>0)&(ds<20000)
    single=(v&(k==2)).sum(); burst=(k[v&(k>=3)]-1).sum(); tot=len(s2)+single+burst
    R['rows']=n0; R['loss_periodic_%']=round(100*single/tot,1); R['loss_burst_%']=round(100*burst/tot,1)
    pair=((ds==19)&np.r_[False,ds[:-1]==11]).sum(); n19=(ds==19).sum()
    if n19>50 and pair/n19>0.9: F.append('server downsample filter signature (11 ms then 19 ms gaps)')
    if R['loss_burst_%']>10: F.append('burst loss above 10%')
    d,info=load_sensor(sid); R['corrupt_rows']=n0-len(d); R['clock_scale']=round(float(info['stm_scale']),4)
    if abs(info['stm_scale']-1)>0.002: F.append('STM clock off by %.1f%% (internal RC oscillator)'%(100*(1-info['stm_scale'])))
    d=d[np.r_[True,np.diff(d.t_wall.values)>0]]; tg=np.arange(d.t_wall.iloc[0],d.t_wall.iloc[-1],0.01); g=lambda c,n=50: sm(np.interp(tg,d.t_wall,pd.Series(d[c].values).rolling(5,center=True,min_periods=1).median().values),n)   # median first: corrupt single rows survive the row filter
    rpm=np.abs(g('inv_motor_speed')); mv=rpm>500; R['moving_s']=int(mv.sum()/100)
    if mv.sum()<3000: R['flags']='not enough driving to check channels'; return R
    ay=g('y_g'); pat=''; alive=[]
    for c in SUS:
        x=g(c); sd=float(np.std(np.interp(tg,d.t_wall,d[c])[mv])); r=float(np.corrcoef(x[mv],ay[mv])[0,1])
        st='dead' if sd<1.0 else ('noisy' if (sd>12 and abs(r)<0.6) else ('+' if r>0.5 else ('-' if r<-0.5 else '?')))
        pat+=f'{c}:{st} '; 
        if st in '+-': alive.append((c,st))
        elif st=='dead': F.append(f'{c} dead (std {sd:.1f} mm while driving)')
        else: F.append(f'{c} not following lateral g (r {r:+.2f}, std {sd:.1f} mm)')
    R['pot_vs_lateral_g']=pat.strip(); sg=dict(alive)
    if len(alive)==4:
        if sg['FR']==sg['RR'] and sg['FL']==sg['RL'] and sg['FR']!=sg['FL']: R['suspension_usable']='yes'
        else:
            R['suspension_usable']='after relabel'; same=[c for c in SUS if sg[c]==sg['FR']]; F.append('left/right labels inconsistent: %s move together, so one front and one rear label are swapped'%'+'.join(same))
    else: R['suspension_usable']='no'
    if 'vehicle_speed_kmh' in d:
        vv=g('vehicle_speed_kmh',100)/3.6; dv=np.gradient(vv,0.01)/9.81; ax=g('x_g',100); m=mv&(vv>3); r=np.corrcoef(dv[m],ax[m])[0,1]; R['x_g_vs_dvdt']=round(float(np.polyfit(dv[m],ax[m],1)[0]),2)
        if r<-0.5: F.append('longitudinal accel sign inverted')
        elif r<0.5: F.append('longitudinal accel does not track speed derivative (r %.2f)'%r)
    st=g('steering_angle'); 
    if np.std(st[mv])>5:
        r=np.corrcoef(st[mv],ay[mv])[0,1]; R['steer_vs_lateral_g']=round(float(r),2)
        if abs(r)<0.5: F.append('steering not correlated with lateral g')
    else: F.append('steering channel flat')
    ib=g('bms_current',100); ii=g('inv_dc_current',100); m=mv&(ib>20)
    if m.sum()>500:
        R['Iinv/Ibms']=round(float(np.median(ii[m]/ib[m])),2)
        if abs(R['Iinv/Ibms']-1)>0.1: F.append('inverter and BMS current disagree by %d%%'%round(100*(R['Iinv/Ibms']-1)))
        T=g('INV_torque_feedback',100) if 'INV_torque_feedback' in d else None
        if T is not None:
            pm=T*rpm*2*np.pi/60; pb=g('bms_voltage',100)*ib; m2=m&(T>30)&(pb>3000)
            if m2.sum()>500:
                R['Pmech/Pbms']=round(float(np.median(pm[m2]/pb[m2])),2)
                if R['Pmech/Pbms']>1.0: F.append('mechanical power exceeds BMS electrical power: one sensor scale is wrong')
    elif float(np.median(g('bms_voltage')))<50: F.append('no BMS data')
    R['flags']='; '.join(F) if F else 'none'; return R
if __name__=='__main__':
    ids=sys.argv[1:]
    if ids==['--all']: ids=[os.path.basename(f)[11:-7] for f in sorted(glob.glob(f'{ROOT}/udp_logger/logs/sensor_log_2026072[8-9]*.csv.gz')+glob.glob(f'{ROOT}/udp_logger/logs/sensor_log_2026073*.csv.gz')+glob.glob(f'{ROOT}/udp_logger/logs/sensor_log_202608*.csv.gz'))]
    out=[r for r in (check(i) for i in ids) if r]
    T=pd.DataFrame(out); T.to_csv(f'{ROOT}/_analysis/out/session_check.csv',index=False)
    with open(f'{ROOT}/_analysis/out/session_check.md','w') as fh:
        for r in out:
            fh.write(f"## {r['session']}\n"+''.join(f"- {k}: {v}\n" for k,v in r.items() if k not in('session','flags'))+'- flags:\n'+''.join(f"  - {x}\n" for x in str(r.get('flags','')).split('; '))+'\n')
    pd.set_option('display.width',260); pd.set_option('display.max_columns',20); pd.set_option('display.max_colwidth',60)
    print(T.drop(columns=['flags']).to_string(index=False)); print(); 
    for r in out: print(r['session'],'|',r.get('flags'))
