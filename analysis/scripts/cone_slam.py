"""Landmark loop closure: dead-reckoned laps drift by metres, so each lap is aligned to the cones of the
reference (best) lap with windowed 2-D ICP (same-colour nearest cones), the corrections are interpolated in
time and applied to the trajectory too. Then all laps are merged into one cone map."""
import numpy as np, pandas as pd, json
from cone_calib import project
p=json.load(open('out/cone_cam_params.json')); Z=np.load('out/cone_prep_0813.npz'); tg,pose,v,laps=Z['tg'],Z['pose'],Z['v'],Z['laps']
o=pd.read_csv('clean/20260813_001309.csv.gz')
LAPS=[k for k,(a,b) in enumerate(laps) if np.diff(o.t[(o.t>=a)&(o.t<=b)].values).max()<=2.5]; REF=int(np.argmin([b-a for a,b in laps]))
D=pd.read_csv('out/cone_dets_0813.csv'); X,Y,d,xl=project(D,p); ok=np.isfinite(d)&(d<15)&(np.abs(xl)<10)
D=D.assign(X=X,Y=Y)[ok&D.lap.isin(LAPS).values].reset_index(drop=True)
print('laps used',LAPS,'reference lap',REF,'detections',len(D))
def peaks(P,B=0.25,sig=0.35,thr=3.0,W=4):
    x0,y0=P.X.min()-2,P.Y.min()-2; nx,ny=int((P.X.max()-x0+2)/B)+1,int((P.Y.max()-y0+2)/B)+1; out=[]
    for col in ('blue','yellow'):
        Q=P[P.color==col]; H=np.zeros((ny,nx)); np.add.at(H,(((Q.Y-y0)/B).astype(int),((Q.X-x0)/B).astype(int)),1)
        k=np.exp(-0.5*(np.arange(-5,6)*B/sig)**2); k/=k.sum()
        H=np.apply_along_axis(lambda r: np.convolve(r,k,'same'),1,H); H=np.apply_along_axis(lambda c: np.convolve(c,k,'same'),0,H)*2*np.pi*sig**2/B**2   # peak height ~ number of detections
        from numpy.lib.stride_tricks import sliding_window_view as sw
        Hp=np.pad(H,W,constant_values=-1); mx=sw(Hp,(2*W+1,2*W+1)).max(axis=(2,3))
        j,i=np.where((H==mx)&(H>=thr)); out+= [(x0+(ii+.5)*B,y0+(jj+.5)*B,col) for jj,ii in zip(j,i)]
    return pd.DataFrame(out,columns=['x','y','color'])
def kabsch(A,B):
    ca,cb=A.mean(0),B.mean(0); H=(A-ca).T@(B-cb); U,S,Vt=np.linalg.svd(H); R=Vt.T@U.T
    if np.linalg.det(R)<0: Vt[1]*=-1; R=Vt.T@U.T
    return R, cb-R@ca
def icp(P,Q,radii=(6,4,3,2,1.5,1.2,1.0,1.0,0.8,0.8)):
    R=np.eye(2); t=np.zeros(2); A=P[['X','Y']].values; cols=P.color.values
    for r in radii:
        A2=A@R.T+t; src=[]; dst=[]
        for col in ('blue','yellow'):
            m=cols==col; q=Q[Q.color==col][['x','y']].values
            if m.sum()==0 or len(q)==0: continue
            dd=np.hypot(A2[m][:,None,0]-q[None,:,0],A2[m][:,None,1]-q[None,:,1]); j=dd.argmin(1); sel=dd[np.arange(len(j)),j]<r
            src.append(A[m][sel]); dst.append(q[j[sel]])
        if not src or sum(len(s) for s in src)<15: return None
        R,t=kabsch(np.vstack(src),np.vstack(dst))
    return R,t,sum(len(s) for s in src)
def align(D,Q,lap,step=3.0,half=4.0):
    a,b=laps[lap]; knots=np.arange(a,b+step/2,step); T=[]
    for k in knots:
        P=D[(D.lap==lap)&(np.abs(D.t-k)<=half)]; r=icp(P,Q) if len(P)>=15 else None
        T.append((np.arctan2(r[0][1,0],r[0][0,0]),*r[1]) if r else (np.nan,np.nan,np.nan))
    T=pd.DataFrame(T,columns=['th','tx','ty']); med=T.rolling(5,center=True,min_periods=2).median()
    bad=(np.hypot(T.tx-med.tx,T.ty-med.ty)>6.0)|(np.abs(T.th-med.th)>0.10); T[bad]=np.nan   # a window with few cones (open straight) can lock onto the wrong row
    nok=int(T.notna().all(1).sum()); T=T.interpolate(limit_direction='both').fillna(0).values; print('   lap',lap,'windows aligned %d/%d'%(nok,len(knots)))
    return knots,T
def apply(knots,T,t,x,y):
    th=np.interp(t,knots,T[:,0]); tx=np.interp(t,knots,T[:,1]); ty=np.interp(t,knots,T[:,2]); c,s=np.cos(th),np.sin(th)
    return c*x-s*y+tx, s*x+c*y+ty
Q=peaks(D[D.lap==REF],thr=4); print('reference-lap cones',len(Q))
corr={}
for rnd in range(4):
    E=D.copy()
    for L in LAPS:
        if rnd==0 and L==REF: continue
        kn,T=align(D,Q,L); corr[L]=(kn,T); m=E.lap==L; E.loc[m,'X'],E.loc[m,'Y']=apply(kn,T,E.t[m].values,D.X[m].values,D.Y[m].values)
    Q=peaks(E,thr=8.0); print('round',rnd,'merged cones',len(Q))
# final map: cones seen in >= 2 laps, cross-lap scatter
rows=[]
for _,c in Q.iterrows():
    G=E[(E.color==c.color)&(np.hypot(E.X-c.x,E.Y-c.y)<0.9)]
    if G.lap.nunique()<2: continue
    lc=G.groupby('lap')[['X','Y']].median()
    rows.append(dict(x=round(float(G.X.median()),2),y=round(float(G.Y.median()),2),color=c.color,n=len(G),laps=int(G.lap.nunique()),lap_scatter_m=round(float(np.sqrt(((lc-lc.mean())**2).sum(axis=1).mean())),2)))
C=pd.DataFrame(rows); C.to_csv('out/cone_map_0813.csv',index=False)
print('final cones',len(C),C.color.value_counts().to_dict(),'| in all %d laps: %d'%(len(LAPS),(C.laps==len(LAPS)).sum()))
print('cross-lap scatter of a cone: median %.2f m, p90 %.2f m'%(C.lap_scatter_m.median(),C.lap_scatter_m.quantile(.9)))
# corrected trajectories and the size of the corrections
TR=[]
for L in LAPS:
    m=pose[:,3]==L; t=tg[m]; x,y=pose[m,0],pose[m,1]
    if L in corr: kn,T=corr[L]; xc,yc=apply(kn,T,t,x,y)
    else: xc,yc=x,y
    TR.append(pd.DataFrame(dict(lap=L,t=t,x=x,y=y,xc=xc,yc=yc,v=v[m])).iloc[::5])
    print('lap',L,'correction applied: median %.1f m, max %.1f m'%(np.median(np.hypot(xc-x,yc-y)),np.hypot(xc-x,yc-y).max()))
pd.concat(TR).to_csv('out/cone_traj_0813.csv',index=False); E.to_csv('out/cone_dets_aligned_0813.csv',index=False)
json.dump(dict(laps=LAPS,ref=REF),open('out/cone_slam_meta.json','w'))
# honest quality measure: share of each lap's detections that land within 1 m of a mapped cone of the same colour
qual={}
for L,G in E.groupby('lap'):
    r=[]
    for col in ('blue','yellow'):
        g=G[G.color==col]; q=C[C.color==col][['x','y']].values
        r.append(np.hypot(g.X.values[:,None]-q[None,:,0],g.Y.values[:,None]-q[None,:,1]).min(1))
    r=np.concatenate(r); qual[int(L)]=dict(median_m=round(float(np.median(r)),2),within_1m=round(float((r<1).mean()),2)); print('lap',int(L),qual[int(L)])
m=json.load(open('out/cone_slam_meta.json')); m['quality']=qual; json.dump(m,open('out/cone_slam_meta.json','w'))
