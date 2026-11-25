import numpy as np, pandas as pd, glob, os, matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
plt.rcParams.update({'font.size':11,'axes.spines.top':False,'axes.spines.right':False,'axes.grid':True,'grid.color':'#e6e6e6','axes.edgecolor':'#888','figure.dpi':130,'axes.titlelocation':'left','axes.titleweight':'bold','axes.titlesize':11})
BLUE='#2a6fdb'; GREY='#8a8a8a'
def med(x,n=5): return pd.Series(x).rolling(n,center=True,min_periods=1).median().values
def sm(x,n): return pd.Series(x).rolling(n,center=True,min_periods=1).mean().values
def load(s):
    o=pd.read_csv(f'clean/{s}.csv.gz'); tg=np.arange(0,o.t.iloc[-1],0.01); G=pd.DataFrame({'t':tg})
    G['ok']=np.interp(tg,o.t.values[1:],np.diff(o.t.values))<0.045
    for c in o.columns:
        if c in('t','stm_ms','gap'): continue
        m=o[c].notna().values
        if m.sum()>10: G[c]=np.interp(tg,o.t[m],med(o[c].values)[m])
    return o,G
rows=[]
for f in sorted(glob.glob('clean/2026*.csv.gz')):
    s=os.path.basename(f)[:15]; o,G=load(s)
    if G.bms_voltage.median()<50: continue
    ok=G.ok.values; v=G.vehicle_speed_kmh.clip(lower=0).values/3.6
    V=G.bms_voltage.values; I=G.bms_current.values; P=V*I
    dist=(v*0.01).sum()/1000; E=(P*0.01).sum()/3600
    # sign check of x_g against speed derivative
    dv=np.gradient(sm(v,100),0.01)/9.81; ax=sm(G.x_g.values,100); mv=(v>3)&ok
    sx=np.corrcoef(dv[mv],ax[mv])[0,1]; kx=np.polyfit(dv[mv],ax[mv],1)[0]
    res={}
    for nm,(vv,ii) in {'bms':(V,I),'inv':(G.inv_voltage.values,G.inv_dc_current.values)}.items():
        vh=sm(vv,30)-sm(vv,2000); ih=sm(ii,30)-sm(ii,2000); m=ok&(np.abs(ih)>5)
        if m.sum()>2000: 
            k=np.polyfit(ih[m],vh[m],1)[0]; res[nm]=(round(-k*1000),round(np.corrcoef(ih[m],vh[m])[0,1],2))
    cmd=G.INV_torque_commanded.values; fb=G.INV_torque_feedback.values; act=(np.abs(cmd)>5)&ok
    rows.append(dict(session=s,dist_km=round(dist,2),E_Wh=round(E),Wh_km=round(E/dist) if dist>1 else None,P_p999_kW=round(np.quantile(P[ok],.999)/1000,1),I_p999=round(np.quantile(I[ok],.999)),V_start=round(np.median(V[:500]),1),V_min=round(np.quantile(V[ok],.001),1),R_bms=res.get('bms'),R_inv=res.get('inv'),tq_p999=round(np.quantile(cmd[ok],.999)),tq_rmse=round(float(np.sqrt(np.mean((cmd[act]-fb[act])**2))),1),tq_r=round(np.corrcoef(cmd[act],fb[act])[0,1],3),ax_vs_dvdt_r=round(sx,2),ax_slope=round(kx,2),ok_pct=round(100*ok.mean())))
P_=pd.DataFrame(rows); pd.set_option('display.width',260); pd.set_option('display.max_columns',40); print(P_.to_string(index=False)); P_.to_csv('out/powertrain.csv',index=False)
# ---------- track reconstruction, 8/13
s='20260813_001309'; o,G=load(s); v=sm(G.vehicle_speed_kmh.clip(lower=0).values/3.6,20); ay=sm(G.y_g.values,20)*9.81
ay=ay-np.median(ay[v<0.5]) if (v<0.5).sum()>100 else ay
r=np.where(v>2.5,ay/np.maximum(v,2.5),0.0); psi=np.cumsum(r)*0.01; x=np.cumsum(v*np.cos(psi))*0.01; y=np.cumsum(v*np.sin(psi))*0.01; sdist=np.cumsum(v)*0.01
print('total dist m',sdist[-1],'net heading turns',psi[-1]/(2*np.pi),'time moving s',(v>2.5).sum()/100)
# curvature profile vs distance -> lap length by autocorrelation
ds=0.5; sg=np.arange(0,sdist[-1],ds); kap=np.interp(sg,sdist,np.where(v>2.5,ay/np.maximum(v,2.5)**2,0.0)); kap=np.clip(kap,-0.5,0.5); kap=kap-kap.mean()
n=len(kap); ac=np.array([np.dot(kap[:n-L],kap[L:])/np.dot(kap,kap)*n/(n-L) for L in range(0,int(1500/ds))])
lagm=np.arange(len(ac))*ds; cand=[(lagm[i],ac[i]) for i in range(40,len(ac)-1) if ac[i]>ac[i-1] and ac[i]>=ac[i+1] and ac[i]>0.25]
print('autocorr peaks (m, r):',[(round(a),round(b,2)) for a,b in sorted(cand,key=lambda z:-z[1])[:8]])
np.save('out/track_0813.npy',np.c_[G.t.values,x,y,v,psi,sdist])
fig,a=plt.subplots(1,2,figsize=(13,5)); sc=a[0].scatter(x[::5],y[::5],c=v[::5]*3.6,s=2,cmap='viridis'); a[0].set_aspect('equal'); a[0].set_title('Dead-reckoned path, 8/13 (speed + lateral g, no GPS)'); a[0].set_xlabel('m'); a[0].set_ylabel('m'); cb=plt.colorbar(sc,ax=a[0],fraction=.04); cb.set_label('km/h'); cb.outline.set_visible(False)
a[1].plot(lagm,ac,color=BLUE); a[1].set_xlabel('distance lag [m]'); a[1].set_ylabel('curvature autocorrelation'); a[1].set_title('Lap length from repeating curvature pattern')
plt.tight_layout(); plt.savefig('out/fig8_track.png'); plt.close()
# ---------- z-accel spectrum by speed band (is the high-frequency content speed dependent?)
z=G.z_g.values; spd=G.vehicle_speed_kmh.values; n=256; w=np.hanning(n); f=np.fft.rfftfreq(n,0.01)
for lo,hi in ((0,5),(20,40),(40,60),(60,95)):
    P=[np.abs(np.fft.rfft((z[a:a+n]-z[a:a+n].mean())*w))**2 for a in range(0,len(z)-n,n//2) if G.ok.values[a:a+n].mean()>0.9 and lo<=spd[a:a+n].mean()<hi]
    if len(P)>5:
        Pm=np.mean(P,axis=0); b=(f>=2)&(f<=45); top=f[b][np.argsort(Pm[b])[-3:]][::-1]; print('z accel %d-%d km/h: segs %d, rms %.3f g, top freqs %s'%(lo,hi,len(P),np.sqrt(Pm[b].sum()/(w**2).sum()/n*2),np.round(top,1)))
