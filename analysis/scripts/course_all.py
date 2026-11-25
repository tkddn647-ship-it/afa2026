"""Per session: lap length from curvature autocorrelation, lap splitting against the session's own best-repeating lap
(curvature + speed profile), and whether the course is the 8/13 course. Speed + lateral g only, no GPS."""
import numpy as np, pandas as pd, glob, os, json
md=lambda x,n=5: pd.Series(x).rolling(n,center=True,min_periods=1).median().values
sm=lambda x,n: pd.Series(x).rolling(n,center=True,min_periods=1).mean().values
DS=0.5
def prof(s):
    o=pd.read_csv(f'clean/{s}.csv.gz'); tg=np.arange(0,o.t.iloc[-1],0.01)
    v=sm(np.interp(tg,o.t,md(o.vehicle_speed_kmh.values)),20).clip(min=0)/3.6; ay=sm(np.interp(tg,o.t,md(o.y_g.values)),20)*9.81
    ay-=np.median(ay[v<0.5]) if (v<0.5).sum()>200 else 0
    sd=np.cumsum(v)*0.01; k=np.clip(np.where(v>2.5,ay/np.maximum(v,2.5)**2,0),-0.5,0.5)
    sg=np.arange(0,sd[-1],DS); P=dict(sg=sg,k=np.interp(sg,sd,k),v=np.interp(sg,sd,v),t=np.interp(sg,sd,tg))
    P['E']=np.interp(sg,sd,np.cumsum(o.bms_voltage.reindex(range(len(o))).values[np.clip(np.searchsorted(o.t.values,tg),0,len(o)-1)]*o.bms_current.values[np.clip(np.searchsorted(o.t.values,tg),0,len(o)-1)])*0.01/3600)
    return P
def z(x): s=x.std(); return (x-x.mean())/s if s>0 else x*0
def feat(P,a,L,sgn=1): return np.r_[z(sgn*P['k'][a:a+L]),z(P['v'][a:a+L])]
def corr(A,B): return float(np.dot(A,B)/len(A))
def acorr(k,maxlag):
    k=k-k.mean(); n=len(k); F=np.fft.rfft(k,2*n); a=np.fft.irfft(F*np.conj(F))[:maxlag]; return a/a[0]*n/(n-np.arange(maxlag))
def scan(T,P,L,sgn=1):
    n=len(P['k']); c=np.full(n,-1.0)
    K=P['k']*sgn; V=P['v']
    for p in range(0,n-L//3):
        m=min(L,n-p); k_=K[p:p+m]; v_=V[p:p+m]; sk=k_.std(); sv=v_.std(); tk=z(T[:L][:m]); tv=z(T[L:][:m])
        if sk>0 and sv>0: c[p]=(np.dot(tk,(k_-k_.mean())/sk)+np.dot(tv,(v_-v_.mean())/sv))/(2*m)   # last lap end: partial window
    return c
def split(P,L):
    n=len(P['k']); best=(-1,0)
    for a in range(0,n-2*L,10):
        cc=corr(feat(P,a,L),feat(P,a+L,L))
        if cc>best[0]: best=(cc,a)
    if best[0]<0.5: return [],best[0],None
    T=feat(P,best[1],L); c=scan(T,P,L); pk=[]; cw=c.copy()
    while True:
        p=int(np.argmax(cw))
        if cw[p]<0.45: break
        pk.append(p); cw[max(0,p-int(0.6*L)):p+int(0.6*L)]=-1
    pk.sort(); laps=[(a,b) for a,b in zip(pk,pk[1:]) if 0.9*L<=b-a<=1.1*L and b<n]
    return laps,best[0],c,pk
sessions=[os.path.basename(f)[:15] for f in sorted(glob.glob('clean/2026*.csv.gz')) if len(os.path.basename(f))==len('20260813_001309.csv.gz')]
P13=prof('20260813_001309'); rows=[]; laps=[]
for s in sessions:
    P=prof(s); dist=P['sg'][-1]
    if dist<1500: continue
    ac=acorr(P['k'],min(int(1600/DS),len(P['k'])//2)); lo=int(250/DS); i=lo+int(np.argmax(ac[lo:])); L=i; peak=float(ac[i])
    r=dict(session=s,distance_km=round(dist/1000,2),lap_len_m=round(L*DS),autocorr=round(peak,2))
    if peak>=0.5:
        lp,c0,cc,pk=split(P,L); r['ref_corr']=round(c0,2)
        # fixed timing line: the fastest point of the lap (main straight), same physical spot for every lap
        if lp:
            vs=np.mean([np.interp(np.linspace(0,1,L),np.linspace(0,1,b-a),sm(P['v'][a:b],int(20/DS))) for a,b in lp],axis=0); off=int(np.argmax(vs))
            bnd=[p+off for p in pk if p+off<len(P['t'])]; ok=[cc[p]>=0.6 for p in pk if p+off<len(P['t'])]
            lp=[(a,b) for (a,b),oa,ob in zip(zip(bnd,bnd[1:]),ok,ok[1:]) if 0.9*L<=b-a<=1.1*L and oa and ob]
            med=np.median([P['t'][b]-P['t'][a] for a,b in lp]) if lp else 0
            lp=[(a,b) for a,b in lp if P['t'][b]-P['t'][a]<=1.12*med]
            json.dump(dict(DS=DS,L=int(L),timing_offset_m=off*DS,laps=[[int(a),int(b)] for a,b in lp]),open(f'out/laps_{s}.json','w'))
            r['timing_line_m_from_ref']=off*DS
        for n,(a,b) in enumerate(lp):
            E=P['E'][b]-P['E'][a]
            laps.append(dict(session=s,lap=n+1,t_start=round(float(P['t'][a]),1),time_s=round(float(P['t'][b]-P['t'][a]),2),length_m=round((b-a)*DS),v_avg=round((b-a)*DS/(P['t'][b]-P['t'][a])*3.6,1),v_max=round(float(P['v'][a:b].max()*3.6),1),energy_Wh=round(float(E),1)))
        r['laps']=len(lp)
    # same course as 8/13? compare speed profile (sign-free) of one lap against the 8/13 lap
    if abs(L*DS-898)<40 and s!='20260813_001309':
        W=int(898/DS); T=z(P13['v'][int(97/DS):int(97/DS)+W]); best=max(corr(T,z(P['v'][a:a+W])) for a in range(0,len(P['v'])-W,4)); r['speed_match_813']=round(best,2)
    rows.append(r); print(r)
pd.DataFrame(rows).to_csv('out/course_all.csv',index=False); LA=pd.DataFrame(laps); LA.to_csv('out/laps_all.csv',index=False)
pd.set_option('display.width',200); pd.set_option('display.max_rows',200)
print(LA.groupby('session').agg(n=('lap','size'),len_med=('length_m','median'),best=('time_s','min'),median=('time_s','median'),worst=('time_s','max'),Wh_lap=('energy_Wh','median')).to_string())

# are the two 7/31 long sessions the same course? speed-profile match of one lap
A=prof('20260731_031858'); B=prof('20260731_034542'); LA_=LA[LA.session=='20260731_031858']
a0=int(np.searchsorted(A['t'],LA_.t_start.iloc[0])); W=int(LA_.length_m.iloc[0]/DS); T=z(A['v'][a0:a0+W])
print('7/31 03:18 lap vs 03:45 speed-profile match: %.2f'%max(corr(T,z(B['v'][a:a+W])) for a in range(0,len(B['v'])-W,4)))
