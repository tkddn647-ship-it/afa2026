import numpy as np, pandas as pd, json, matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
from common import *
plt.rcParams.update({'font.size':11,'axes.spines.top':False,'axes.spines.right':False,'axes.grid':True,'grid.color':'#e6e6e6','axes.edgecolor':'#888','figure.dpi':130,'axes.titlelocation':'left','axes.titleweight':'bold','axes.titlesize':11})
sm=lambda x,n: pd.Series(x).rolling(n,center=True,min_periods=1).mean().values
md=lambda x,n=5: pd.Series(x).rolling(n,center=True,min_periods=1).median().values
H={h['session']:h['video_to_log_lag_s'] for h in json.load(open('out/offset_hints.json'))}
K_RAD_PX=0.001858; P=[]
for sid in ['20260811_022758_150378','20260811_073314_333336','20260811_084517_257245','20260813_001309_427966']:
    d,_=load_sensor(sid); d=d[np.r_[True,np.diff(d.t_wall.values)>0]]
    y=pd.read_csv(f'out/yaw_{sid[4:15]}.csv'); tv=video_t0(sid)+y.t_ms.values/1000+H[sid]
    dx=pd.Series(np.where((np.abs(y.dx)>80)|(y.resp<0.03),np.nan,y.dx)).interpolate(limit=8).fillna(0).values
    f=lambda c: np.interp(tv,d.t_wall,sm(md(d[c].values),50))
    rc=sm(dx*K_RAD_PX*15.0,8); st=f('steering_angle'); v=f('vehicle_speed_kmh')/3.6; ay=f('y_g')*9.81; ra=ay/np.maximum(v,1)
    rc*=np.sign(np.corrcoef(rc[v>5],st[v>5])[0,1]); ra*=np.sign(np.corrcoef(ra[v>5],st[v>5])[0,1])
    q=(v>5)&(np.abs(np.gradient(st,1/15))<20)&(np.abs(np.gradient(sm(v,15),1/15))<1.5)&(np.abs(st)<120)
    P.append(pd.DataFrame({'s':sid[:15],'v':v[q],'st':st[q],'rc':rc[q],'ra':ra[q]}))
P=pd.concat(P); bins=[5,7,9,11,13,16,20,26]
def fit(Q,col):
    out=[]
    for lo,hi in zip(bins[:-1],bins[1:]):
        q=Q[(Q.v>=lo)&(Q.v<hi)]
        if len(q)<150 or np.ptp(q.st)<30: continue
        k,b=np.polyfit(np.radians(q.st),q[col],1); r=np.corrcoef(q.st,q[col])[0,1]
        out.append(dict(v=q.v.median(),G=k,offset_deg=-np.degrees(b/k),r=r,n=len(q)))
    T=pd.DataFrame(out); T['Leff']=T.v/T.G; return T
for col,nm in (('rc','camera yaw'),('ra','lateral g / speed')):
    T=fit(P,col); co=np.polyfit(T.v**2,T.Leff,1); print('==',nm); print(T.round(3).to_string(index=False)); print('   Leff = %.2f + %.4f v^2 -> K = %.4f s2/m2'%(co[1],co[0],co[0]/co[1]))
    # session-level jackknife
    js=[]
    for s in P.s.unique():
        TT=fit(P[P.s!=s],col); c=np.polyfit(TT.v**2,TT.Leff,1); js.append((s[4:13],round(c[1],2),round(c[0]/c[1],4)))
    print('   leave-one-session-out (Leff0, K):',js)
    if col=='rc': Tc,coc=T,co
    else: Ta,coa=T,co
fig,ax=plt.subplots(figsize=(8.5,4.6)); vv=np.linspace(5,22,50)
ax.plot(Tc.v,Tc.Leff,'o',color='#2a6fdb',label='camera yaw rate'); ax.plot(vv,np.polyval(coc,vv**2),color='#2a6fdb',lw=1)
ax.plot(Ta.v,Ta.Leff,'s',color='#8a8a8a',label='lateral g / speed'); ax.plot(vv,np.polyval(coa,vv**2),color='#8a8a8a',lw=1,ls='--')
ax.set_xlabel('speed [m/s]'); ax.set_ylabel('speed / yaw gain  [m]\n(= steer ratio x wheelbase x (1 + K v$^2$))'); ax.set_title('Steady-state steering response rises with speed: understeer'); ax.legend(frameon=False)
plt.tight_layout(); plt.savefig('out/fig11_yawgain.png'); plt.close()
# ---- single model with one steering offset per session: r = v*(delta - d0_s) / (L0*(1+K v^2))
def fit_model(Q,col):
    best=None
    for K in np.linspace(-0.002,0.01,61):
        # for fixed K the model is linear in a=1/L0 and b_s=-d0_s/L0
        ss=list(Q.s.unique()); X=np.zeros((len(Q),1+len(ss))); w=Q.v.values/(1+K*Q.v.values**2)
        X[:,0]=w*np.radians(Q.st.values)
        for j,s in enumerate(ss): X[:,1+j]=w*(Q.s.values==s)
        co,res,_,_=np.linalg.lstsq(X,Q[col].values,rcond=None); sse=((X@co-Q[col].values)**2).sum()
        if best is None or sse<best[0]: best=(sse,K,1/co[0],{s[4:13]:round(float(-np.degrees(co[1+j]*(1/co[0]))),1) for j,s in enumerate(ss)})
    return best
for col,nm in (('rc','camera'),('ra','ay/v')):
    b=fit_model(P,col); print('MODEL',nm,': L0 %.2f m, K %.4f s2/m2, steering offsets deg %s'%(b[2],b[1],b[3]))
    for s in P.s.unique():
        bb=fit_model(P[P.s!=s],col); print('   without',s[4:13],': L0 %.2f K %.4f'%(bb[2],bb[1]))
# ---- figure from the single model: correct each session's steering zero, then gain per speed bin through the origin
bc=fit_model(P,'rc'); ba=fit_model(P,'ra'); 
Q=P.copy()
Q['stc']=np.radians(P.st.values-np.array([bc[3][s[4:13]] for s in P.s]))
rows=[]
for lo,hi in zip(bins[:-1],bins[1:]):
    q=Q[(Q.v>=lo)&(Q.v<hi)]
    if len(q)<150: continue
    for col in ('rc','ra'):
        g=np.dot(q.stc,q[col])/np.dot(q.stc,q.stc); rows.append(dict(col=col,v=q.v.median(),L=q.v.median()/g,n=len(q)))
R=pd.DataFrame(rows); print(R.round(2).to_string(index=False))
fig,ax=plt.subplots(figsize=(8.5,4.6)); vv=np.linspace(5,24,60)
for col,b,c,mk,lab in (('rc',bc,'#2a6fdb','o','camera yaw rate'),('ra',ba,'#8a8a8a','s','lateral g / speed')):
    r_=R[R.col==col]; ax.plot(r_.v,r_.L,mk,color=c,label=f'{lab}: L0 {b[2]:.1f} m, K {b[1]:.4f} s$^2$/m$^2$'); ax.plot(vv,b[2]*(1+b[1]*vv**2),color=c,lw=1.2,ls='-' if col=='rc' else '--')
vch=1/np.sqrt(bc[1]); ax.axvline(vch,color='#cccccc',lw=1); ax.text(vch+0.3,ax.get_ylim()[0]+0.5,f'characteristic speed {vch:.0f} m/s',color='#555',fontsize=9)
ax.set_xlabel('speed [m/s]'); ax.set_ylabel('speed / yaw-rate gain [m]'); ax.set_title('Steering needed per unit yaw rate grows with speed: understeer, 8/11-8/13'); ax.legend(frameon=False,fontsize=9,loc='upper left')
plt.tight_layout(); plt.savefig('out/fig11_yawgain.png'); plt.close()
json.dump(dict(camera=dict(L0=bc[2],K=bc[1],offsets=bc[3]),ayv=dict(L0=ba[2],K=ba[1],offsets=ba[3])),open('out/yaw_model.json','w'),indent=1)
