# macro-dashboard

แดชบอร์ด macro ที่ **อัพเดทเองทุกวันด้วย GitHub Actions** (ฟรี, ไม่ต้องมี API key)

- `scripts/fetch_macro.py` — engine ดึง FRED keyless + คำนวณ (z-score, quadrant, trail, sparks)
- `scripts/build_dashboard.py` — สร้าง `index.html` แบบ self-contained จากข้อมูลสด
- `assets/macro_dashboard.html` — เทมเพลต (มี marker `/* DATA_START */ … /* DATA_END */`)
- `.github/workflows/update-dashboard.yml` — รันทุกวัน 12:00 UTC: build → commit `index.html`
- `index.html` — ไฟล์ที่ GitHub Pages เสิร์ฟ (สร้างโดย Action / รันเองในเครื่อง)

## รันในเครื่อง (มี Python 3)
```
python scripts/build_dashboard.py --out index.html
```
แล้วเปิด `index.html` ด้วยเบราว์เซอร์

## ตั้งครั้งเดียวให้ auto บน GitHub
1. สร้าง repo ใหม่ชื่อ `macro-dashboard` บน github.com (public)
2. push โฟลเดอร์นี้ขึ้นไป (ดูคำสั่งใน chat)
3. **Settings → Pages** → Source = `Deploy from a branch`, Branch = `main` / `/ (root)` → Save
4. **Settings → Actions → General** → อนุญาต workflow + Read and write permissions
5. แท็บ **Actions** → รัน "Update macro dashboard" หนึ่งครั้ง (Run workflow)
6. เปิดดูที่ `https://<username>.github.io/macro-dashboard/` — จากนั้นอัพเดทเองทุกวัน

> ข้อมูลเพื่อการศึกษา ไม่ใช่คำแนะนำการลงทุน
