"""Forward motion of the ground in the 8/13 forward video. Features on the asphalt (rows 300-470) are tracked frame to
frame (pyramidal Lucas-Kanade, forward-backward checked). On flat ground a point at image row y lies d = F*h/(y - y_h)
ahead, so each track gives the forward travel in units of the camera height h: F/(y1-yh) - F/(y2-yh). Median per frame."""
import cv2, numpy as np, json, sys, csv
p = json.load(open('out/cone_cam_params.json')); F, CX = 538.0, 427.0
src, dst = sys.argv[1], sys.argv[2]; t0 = float(sys.argv[3]); t1 = float(sys.argv[4])
cap = cv2.VideoCapture(src); cap.set(cv2.CAP_PROP_POS_MSEC, t0 * 1000); i = int(round(t0 * 15))
w = csv.writer(open(dst, 'w', newline='')); w.writerow(['frame', 'n', 'ds_h', 'ds_h_iqr'])
yh = lambda u: p['yh'] + (u - CX) * p['tilt'] / CX
lk = dict(winSize=(21, 21), maxLevel=4, criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01))
mask = np.zeros((480, 854), np.uint8); mask[300:470, 40:814] = 255
ok, prev = cap.read(); prev = cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY)
while ok:
    ok, fr = cap.read()
    if not ok or i / 15 >= t1: break
    g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
    pts = cv2.goodFeaturesToTrack(prev, maxCorners=160, qualityLevel=0.01, minDistance=8, mask=mask)
    if pts is not None and len(pts) > 10:
        nxt, st, _ = cv2.calcOpticalFlowPyrLK(prev, g, pts, None, **lk); back, st2, _ = cv2.calcOpticalFlowPyrLK(g, prev, nxt, None, **lk)
        good = (st.ravel() == 1) & (st2.ravel() == 1) & (np.linalg.norm(back - pts, axis=2).ravel() < 1.0)
        a, b = pts.reshape(-1, 2)[good], nxt.reshape(-1, 2)[good]
        r1, r2 = a[:, 1] - yh(a[:, 0]), b[:, 1] - yh(b[:, 0]); m = (r1 > 40) & (r2 > 40) & (b[:, 1] < 478)
        if m.sum() >= 8:
            ds = F / r1[m] - F / r2[m]; q = np.percentile(ds, [25, 50, 75]); w.writerow([i + 1, int(m.sum()), round(q[1], 5), round(q[2] - q[0], 5)])
    prev = g; i += 1
print('done to frame', i)
