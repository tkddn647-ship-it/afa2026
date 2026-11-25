import numpy as np, pandas as pd, glob, os, json
def med(x,n=5): return pd.Series(x).rolling(n,center=True,min_periods=1).median().values
def sm(x,n): return pd.Series(x).rolling(n,center=True,min_periods=1).mean().values
def load(s):
    o=pd.read_csv(f'clean/{s}.csv.gz'); tg=np.arange(0,o.t.iloc[-1],0.01); G=pd.DataFrame({'t':tg})
    G['ok']=np.interp(tg,o.t.values[1:],np.diff(o.t.values))<0.045
    for c in o.columns:
        if c in('t','stm_ms','gap'): continue
        m=o[c].notna().values
        if m.sum()>10: G[c]=np.interp(tg,o.t[m],med(o[c].values)[m])
    return G
SESS=[os.path.basename(f)[:15] for f in sorted(glob.glob('clean/2026*.csv.gz'))]
GEAR,RT=3.9,0.23
eff=[]; mass=[]; coast=[]; therm=[]; steer=[]
EB=[]; 
for s in SESS:
    G=load(s); ok=G.ok.values
    if G.bms_voltage.median()<50: continue
    n=100; T=sm(G.INV_torque_feedback.values,n); rpm=sm(G.inv_motor_speed.values,n); w=rpm*2*np.pi/60
    Pm=T*w; Pi=sm(G.inv_voltage.values*G.inv_dc_current.values,n); Pb=sm(G.bms_voltage.values*G.bms_current.values,n)
    stdy=ok&(T>30)&(rpm>800)&(np.abs(np.gradient(T,0.01))<40)&(Pb>3000)&(Pi>3000)
    if stdy.sum()>800:
        r=dict(session=s,n_s=round(stdy.sum()/100),eff_vs_inverter_current=round(float(np.median(Pm[stdy]/Pi[stdy])),3),eff_vs_bms_current=round(float(np.median(Pm[stdy]/Pb[stdy])),3),Iinv_over_Ibms=round(float(np.median(sm(G.inv_dc_current.values,n)[stdy]/sm(G.bms_current.values,n)[stdy])),3),Vinv_over_Vbms=round(float(np.median(G.inv_voltage.values[stdy]/G.bms_voltage.values[stdy])),3))
        # energy-ratio version (insensitive to timing)
        r['eff_energy_inv']=round(float(Pm[stdy].sum()/Pi[stdy].sum()),3); r['eff_energy_bms']=round(float(Pm[stdy].sum()/Pb[stdy].sum()),3); eff.append(r)
        EB.append(pd.DataFrame({'rpm':rpm[stdy][::20],'T':T[stdy][::20],'eb':(Pm/Pb)[stdy][::20],'ei':(Pm/Pi)[stdy][::20]}))
    # ---- mass from longitudinal dynamics (speed from motor rpm)
    v=sm(G.vehicle_speed_kmh.clip(lower=0).values/3.6,50); a=np.gradient(v,0.01); ay=sm(G.y_g.values,50); Tq=sm(G.INV_torque_feedback.values,50); F=Tq*GEAR/RT
    m=ok&(Tq>15)&(v>3)&(np.abs(ay)<0.3)&(a>0.3)
    if m.sum()>800:
        X=np.c_[a[m],v[m]**2,np.ones(m.sum())]; co=np.linalg.lstsq(X,F[m],rcond=None)[0]; pred=X@co; r2=1-((F[m]-pred)**2).sum()/((F[m]-F[m].mean())**2).sum()
        # accelerometer-based alternative (sign from correlation)
        ax=sm(G.x_g.values,50)*9.81; sgn=np.sign(np.corrcoef(ax[m],a[m])[0,1]); X2=np.c_[sgn*ax[m],v[m]**2,np.ones(m.sum())]; c2=np.linalg.lstsq(X2,F[m],rcond=None)[0]
        mass.append(dict(session=s,n_s=round(m.sum()/100),m_eff_kg_from_rpm=round(float(co[0])),v2_coef=round(float(co[1]),2),F0_N=round(float(co[2])),r2=round(float(r2),2),m_eff_kg_from_accel=round(float(c2[0]))))
    # ---- coast-down: zero torque command, gentle decel, straight
    Tc=sm(G.INV_torque_commanded.values,30); c=ok&(np.abs(Tc)<1.0)&(np.abs(Tq)<3)&(v>5)&(np.abs(ay)<0.15)&(a<-0.05)&(a>-1.5)
    if c.sum()>300: coast.append(pd.DataFrame({'s':s,'v':v[c][::10],'a':a[c][::10]}))
    # ---- thermal
    g=lambda c_: (round(float(np.median(G[c_].values[:300])),1),round(float(np.quantile(G[c_].values[ok],.999)),1),round(float(np.median(G[c_].values[-300:])),1))
    E=(G.bms_voltage*G.bms_current*0.01).sum()/3.6e6
    therm.append(dict(session=s,dur_min=round(len(G)/6000,1),E_kWh=round(float(E),2),igbt=g('inv_temp_igt'),motor=g('inv_temp_motor'),batt=g('bms_temp_maxvalue')))
    # ---- steering model: delta_sw = A*(ay/v^2) + B*ay + c
    st=sm(G.steering_angle.values,30); ayy=sm(G.y_g.values,30)*9.81; vv=sm(G.vehicle_speed_kmh.values/3.6,30); m2=ok&(vv>7)&(np.abs(ayy)>1.0)&(np.abs(np.gradient(st,0.01))<60)
    if m2.sum()>1500 and np.std(st[m2])>5:
        X=np.c_[ayy[m2]/vv[m2]**2,ayy[m2],np.ones(m2.sum())]; co=np.linalg.lstsq(X,np.radians(st[m2]),rcond=None)[0]; pred=X@co; r2=1-((np.radians(st[m2])-pred)**2).sum()/((np.radians(st[m2])-np.radians(st[m2]).mean())**2).sum()
        steer.append(dict(session=s,n_s=round(m2.sum()/100),ratio_x_wheelbase_m=round(float(co[0]),2),understeer_sw_deg_per_g=round(float(np.degrees(co[1])*9.81),1),r2=round(float(r2),2)))
pd.set_option('display.width',250); pd.set_option('display.max_columns',30)
for nm,t in (('EFFICIENCY',eff),('MASS',mass),('THERMAL',therm),('STEER',steer)): print('==',nm); print(pd.DataFrame(t).to_string(index=False)); pd.DataFrame(t).to_csv(f'out/{nm.lower()}.csv',index=False)
Cd=pd.concat(coast); print('== COAST samples',len(Cd),'sessions',Cd.s.nunique()); b=pd.cut(Cd.v*3.6,[18,30,40,50,60,70,95]); print(Cd.groupby(b,observed=True).a.agg(['median','size']).round(3).to_string())
X=np.c_[Cd.v**2,np.ones(len(Cd))]; co=np.linalg.lstsq(X,-Cd.a.values,rcond=None)[0]; print('coast decel = %.4f*v^2 + %.3f m/s^2 ; per kg: CdA/m=%.5f m2/kg (rho 1.2), a0=%.3f'%(co[0],co[1],2*co[0]/1.2,co[1])); Cd.to_csv('out/coast.csv',index=False)
EBd=pd.concat(EB); EBd.to_csv('out/eff_points.csv',index=False)
EBd['rb']=pd.cut(EBd.rpm,[800,1500,2500,3500,4500]); EBd['tb']=pd.cut(EBd['T'],[30,60,100,150,240]); print('== efficiency (BMS power) by rpm x torque, median'); print(EBd.pivot_table(index='tb',columns='rb',values='eb',aggfunc='median',observed=True).round(2).to_string()); print(EBd.pivot_table(index='tb',columns='rb',values='eb',aggfunc='size',observed=True).to_string())
