# 🚀 Index Bot Web (Fixed)

Web app ép Google index toàn bộ URL từ sitemap WordPress.

## Fixes
- Sử dụng WebSocket với wss:// khi deploy Railway (HTTPS).
- Log realtime: kết nối, error, đóng.
- Hiển thị tiến trình từng URL với số thứ tự.

## Cách chạy
1. Cài biến môi trường:
   - `API1_JSON`...`API5_JSON` = JSON Service Account 1 dòng.
2. Deploy lên Railway:
   - Push code này lên GitHub.
   - Kết nối Railway → New Project → Deploy from Repo.
3. Mở web → nhập domain → chọn API → chạy index.

