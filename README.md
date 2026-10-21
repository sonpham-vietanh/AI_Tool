# AI Book Translator Hub (Multi-Agent Studio)

Hệ thống **Tìm kiếm Đa Phiên Bản (EPUB / PDF / DOCX / Bản Leak)** & **Dịch Thuật Sách Tự Động Đa Agent** giữ nguyên định dạng gốc.

## Tính năng cốt lõi

1. **Agent 1 — Săn lùng Đa Phiên Bản & Bản Leak (`backend/search/providers.py`):**
   - Tìm kiếm song song trên 5 nguồn: **Web/GitHub/Google Drive Leaks**, **Internet Archive / OpenLibrary Deep File Resolver**, **LibGen**, **Anna's Archive**, và **Project Gutenberg**.
   - Hỗ trợ tự động mở rộng tên sách tiếng Việt sang tên gốc tiếng Anh để săn đủ các lần tái bản (1st/2nd/3rd Edition, bản PDF/EPUB).
   - Bảng chọn ấn bản trực quan (Bước 1) trước khi đưa vào dây chuyền dịch thuật (Bước 2).
2. **Dây chuyền 5 Agent Dịch thuật (`backend/llm/` & `backend/translators/`):**
   - **Agent 2 (Lexicographer):** Trích xuất thuật ngữ & lập từ điển (Glossary) toàn sách.
   - **Agent 3 (Main Translator):** Dịch thuật song song đa luồng bám sát ngữ cảnh chương trước và Glossary.
   - **Agent 4 (Literary Editor):** Hiệu đính văn phong tiếng Việt mượt mà, tự nhiên.
   - **Agent 5 (Typesetter):** Đóng gói lại định dạng gốc (`EPUB`, `DOCX`, `PDF`, `TXT`) hoặc xuất bản song ngữ.

## Khởi chạy nhanh (Windows)

```bat
pip install -r requirements.txt
start_hub.bat
```

Hoặc chạy trực tiếp bằng Uvicorn:

```bash
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8088 --reload
```

Sau đó truy cập: **http://localhost:8088**

## Triển khai lên VPS (Docker / Coolify)

Repo có sẵn `Dockerfile`. App **không có tài khoản người dùng riêng**. Khi mở ra internet có 2 chế độ:

1. **Có đăng nhập (khuyên dùng):** đặt `BTH_AUTH_PASSWORD`. Toàn bộ giao diện và API đòi HTTP Basic.
2. **Không đăng nhập:** đặt `BTH_DISABLE_AUTH=1`. Ai có link cũng dùng được và **tốn credit LLM của bạn**. Để key không bị đánh cắp, khi chạy công khai không đăng nhập app **khóa cài đặt key/endpoint**: truyền `BTH_API_KEY` và `BTH_BASE_URL` bằng biến môi trường.

App từ chối khởi động nếu mở ra ngoài mà không chọn một trong hai.

**Biến môi trường** (xem `.env.example`):

| Biến | Ý nghĩa |
|---|---|
| `BTH_ALLOWED_HOSTS` | Domain được truy cập, VD `books.example.com` (nhiều domain cách nhau bằng dấu phẩy) |
| `BTH_AUTH_USER` / `BTH_AUTH_PASSWORD` | Đăng nhập HTTP Basic cho toàn bộ giao diện và API |
| `BTH_DISABLE_AUTH` | `1` = cố ý không đăng nhập (xem cảnh báo ở trên) |
| `BTH_DATA_DIR` | Thư mục dữ liệu (mặc định `/data` trong Docker) |
| `BTH_API_KEY`, `BTH_BASE_URL` | Key và endpoint LLM mặc định. **Bắt buộc** ở chế độ không đăng nhập |

**Các bước trên Coolify:**
1. New Application → nguồn Git (repo này, branch `main`), Build Pack: **Dockerfile**, cổng **8088**.
2. Environment Variables: đặt các biến ở trên.
3. Persistent Storage: mount một volume vào **`/data`** (nếu không, mất cache dịch, job và file sau mỗi lần redeploy).
4. Domains: gắn domain trùng `BTH_ALLOWED_HOSTS`, bật HTTPS.
5. Deploy. Healthcheck dùng `/api/ping` (không cần đăng nhập).
