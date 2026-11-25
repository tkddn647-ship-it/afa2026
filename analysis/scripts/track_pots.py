"""Track the green end-caps of the two front linear potentiometers in the onboard video.
Output: per-frame centroids of 4 green markers (L_outer, L_inner, R_inner, R_outer) in pixels."""
import cv2, numpy as np, sys, csv, time
src, dst = sys.argv[1], sys.argv[2]
X0, X1, Y0, Y1 = 315, 625, 280, 350          # ROI containing both pots
cap = cv2.VideoCapture(src)
w = csv.writer(open(dst, 'w', newline=''))
w.writerow(['frame', 't_ms', 'n_blobs'] + [f'{n}_{c}' for n in ('Lo', 'Li', 'Ri', 'Ro') for c in ('x', 'y', 'area')] + ['sharp'])
i = 0; t0 = time.time()
while True:
    ok, fr = cap.read()
    if not ok: break
    t = cap.get(cv2.CAP_PROP_POS_MSEC)
    roi = fr[Y0:Y1, X0:X1]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    m = cv2.inRange(hsv, (45, 90, 70), (90, 255, 255))
    n, lab, st, cen = cv2.connectedComponentsWithStats(m, connectivity=8)
    blobs = [(st[k, cv2.CC_STAT_AREA], k) for k in range(1, n) if st[k, cv2.CC_STAT_AREA] >= 6]
    blobs.sort(reverse=True); blobs = blobs[:4]
    row = [i, round(t, 2), len(blobs)]
    if len(blobs) == 4:
        pts = []
        sat = hsv[:, :, 1].astype(np.float32)
        for a, k in blobs:
            ys, xs = np.nonzero(lab == k); wgt = sat[ys, xs]
            pts.append((float((xs * wgt).sum() / wgt.sum()) + X0, float((ys * wgt).sum() / wgt.sum()) + Y0, int(a)))
        pts.sort()
        for p in pts: row += [round(p[0], 3), round(p[1], 3), p[2]]
    else:
        row += [''] * 12
    row.append(round(float(cv2.Laplacian(cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY), cv2.CV_32F).var()), 1))
    w.writerow(row); i += 1
print('frames', i, 'sec', round(time.time() - t0, 1))
